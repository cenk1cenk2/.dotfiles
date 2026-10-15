"""Quiet other applications' playback while a script makes noise of its own.

This module only ever touches audio that belongs to somebody else. Sound this
machine emits on its own behalf - the alert tone - is a notification, and lives
with the other notifications in `notify`.

Two halves, because no single mechanism covers everything. Players that speak
MPRIS are paused outright: ducking a podcast to a third still loses the words,
and pausing resumes at the same position with every volume untouched. Whatever
is left — games, calls, anything with no MPRIS — is ducked instead.

Ducking works on sink inputs — the per-application streams — never on the sink
itself. A sink's volume is the output device's master volume, which desktop
shells (swayosd, swaync, ...) watch and answer with a volume OSD on every
utterance, and a crash while ducked would leave the speakers wrong until
someone noticed.

Streams are snapshotted once, at duck time, so anything that starts while
ducked plays at full volume. Catching late arrivals needs a sink-input event
subscription; until then that is the price of leaving master volume alone.

WirePlumber remembers every stream volume per application, ducked ones
included, so a stream that ends while ducked hands its quieted level to the
next stream that app opens. Nothing outside WirePlumber can rewrite that
memory - pipewire-pulse's stream-restore extension reaches it only for the
notification role - so the fix waits until the app plays again: the app is
recorded as owed, and the next suppression or restore lifts its stream from
the ducked level. `SuppressJournal` carries that, and what a run that died
mid-suppression left behind."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import subprocess
import threading
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

import pulsectl

from .desktop import is_headless

DEFAULT_DUCK_FACTOR = 0.3

log = logging.getLogger(__name__)

DEFAULT_SINK = "@DEFAULT_AUDIO_SINK@"
DEFAULT_SOURCE = "@DEFAULT_AUDIO_SOURCE@"


def muted(node: str) -> bool:
    """Whether a WirePlumber node is muted, by id or `DEFAULT_SINK` / `DEFAULT_SOURCE`.

    `wpctl` takes no node name. Unreadable answers no, so a broken probe never
    stands in the way of the audio it is asked about."""
    cmd = ["wpctl", "get-volume", node]
    log.debug("spawn: %s", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=2
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("mute check failed: %s", e)
        return False
    if proc.stderr:
        log.debug("wpctl stderr: %s", proc.stderr.strip())

    return "[MUTED]" in proc.stdout

@dataclass(frozen=True)
class DuckedStream:
    """A stream's volume as it was, plus enough to know nothing else moved it."""

    identity: tuple[str | None, ...]
    values: list[float]
    applied: list[float]

    @property
    def app(self) -> str | None:
        """The name WirePlumber remembers this stream's volume against."""
        return self.identity[2] or self.identity[1]

    def is_applied(self, current: list[float]) -> bool:
        """True while the stream still sits at the level `duck` set.

        Volumes come back as floats and PulseAudio rounds through integer
        units, so an exact comparison would reject a stream nobody touched."""
        return len(current) == len(self.applied) and all(
            abs(now - was) <= 0.01 for now, was in zip(current, self.applied)
        )

    @classmethod
    def load(cls, data: dict) -> DuckedStream:
        return cls(tuple(data["identity"]), data["values"], data["applied"])

class SuppressJournal:
    """What a suppression changed, kept on disk until it is undone.

    One file per process, named for its pid and stamped with the boot it was
    written in, so a file whose process is gone or whose boot has passed is a
    run that died without restoring. `owed.json` is shared: the apps whose
    ducked stream ended before it could be put back. It lives under the state
    dir rather than the runtime dir because WirePlumber's memory of the ducked
    volume survives a reboot too."""

    ROOT = (
        Path(os.environ.get("XDG_STATE_HOME") or "~/.local/state").expanduser()
        / "dotlib"
        / "suppress"
    )
    OWED = ROOT / "owed.json"
    BOOT = Path("/proc/sys/kernel/random/boot_id")

    def __init__(self):
        self.path = self.ROOT / f"{os.getpid()}.json"
        self.boot = self.BOOT.read_text().strip()
        self.streams: dict[int, DuckedStream] = {}
        self.players: list[str] = []

    @staticmethod
    def _write(path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(path)

    @staticmethod
    def _read(path: Path) -> dict | None:
        try:
            return json.loads(path.read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            log.warning("unreadable suppress journal %s: %s", path, e)
            return None

    def save(self) -> None:
        """Write this process's record, or drop it once nothing is left to undo."""
        if not self.streams and not self.players:
            self.path.unlink(missing_ok=True)
            return

        self._write(
            self.path,
            {
                "boot": self.boot,
                "streams": {
                    str(index): asdict(entry) for index, entry in self.streams.items()
                },
                "players": self.players,
            },
        )

    def _is_live(self, path: Path, data: dict) -> bool:
        if data.get("boot") != self.boot:
            return False
        try:
            os.kill(int(path.stem), 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            pass

        return True

    def others(self) -> Iterator[tuple[Path, dict, bool]]:
        """Every other process's record, and whether that process still runs."""
        for path in self.ROOT.glob("*.json"):
            if path == self.path or not path.stem.isdigit():
                continue
            if (data := self._read(path)) is not None:
                yield path, data, self._is_live(path, data)

    def live_indices(self) -> set[int]:
        """Streams another running suppression has ducked and will put back."""
        return {
            int(index)
            for _, data, live in self.others()
            if live
            for index in data.get("streams", {})
        }

    def owed(self) -> dict[str, DuckedStream]:
        data = self._read(self.OWED) or {}

        return {app: DuckedStream.load(entry) for app, entry in data.items()}

    def save_owed(self, owed: dict[str, DuckedStream]) -> None:
        if not owed:
            self.OWED.unlink(missing_ok=True)
            return

        self._write(self.OWED, {app: asdict(entry) for app, entry in owed.items()})

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Serialise recovery across processes, so a dead record is replayed once."""
        self.ROOT.mkdir(parents=True, exist_ok=True)
        with open(self.ROOT / ".lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

class Ducker:
    """Scales every other playback stream down, and puts each one back.

    The factor multiplies each channel against that stream's own current
    volume, so one already turned down is quieted in proportion rather than
    dragged to a level it shares with everything else. Restoring is
    per-channel and verbatim, so a stream that was panned or unbalanced comes
    back the way its owner left it."""

    def __init__(
        self,
        factor: float = DEFAULT_DUCK_FACTOR,
        *,
        name: str = "dotlib",
        exclude: Collection[str] = (),
        journal: SuppressJournal | None = None,
    ):
        self._factor = min(max(factor, 0.0), 1.0)
        self._name = name
        self._exclude = {value.lower() for value in exclude}
        self._journal = journal
        self._ducked: dict[int, DuckedStream] = {}
        self._lock = threading.Lock()

    def _record(self) -> None:
        if self._journal is not None:
            self._journal.streams = dict(self._ducked)
            self._journal.save()

    @staticmethod
    def _identity(stream: pulsectl.PulseSinkInputInfo) -> tuple[str | None, ...]:
        props = stream.proplist
        return (
            props.get("application.process.id"),
            props.get("application.process.binary"),
            props.get("application.name"),
        )

    def _is_excluded(
        self, stream: pulsectl.PulseSinkInputInfo, extra: Collection[str] = ()
    ) -> bool:
        """True for a stream the caller named as one to leave alone.

        The stream-restore database is keyed on the application name, so
        ducking someone else's `ffplay` and then spawning our own hands the
        new stream the ducked volume — snapshotting before the spawn keeps it
        out of `restore`, but not out of that lookup. PipeWire-native clients
        set only `application.name`, so both keys have to be checked."""
        props = stream.proplist
        binary = (props.get("application.process.binary") or "").lower()
        app = (props.get("application.name") or "").lower()

        return any(
            name in names
            for names in (self._exclude, extra)
            for name in (binary, app)
            if name
        )

    @property
    def is_ducked(self) -> bool:
        with self._lock:
            return bool(self._ducked)

    def duck(self, exclude: Collection[str] = ()) -> int:
        """Scale every live playback stream by `factor`; returns how many.

        `exclude` names streams to skip on top of the constructor's set, for
        callers that learn what to leave alone only at duck time.

        Corked streams are skipped. A paused stream is worth nothing to duck
        and costs something: if it goes away before `restore` runs, the
        stream-restore database keeps the ducked volume against that app and
        hands it back to the next stream it opens.

        Streams another running suppression already ducked are skipped too:
        ducking them again would snapshot the quieted level as the original,
        and the run that restores last would leave them quiet.

        The journal is written before any volume moves, so a death mid-sweep
        still leaves a record; an entry whose volume never moved fails the
        `is_applied` check on replay and is left alone."""
        if is_headless():
            log.debug("headless: skipping duck")
            return 0

        extra = {value.lower() for value in exclude}
        taken = self._journal.live_indices() if self._journal is not None else set()
        with self._lock:
            if self._ducked:
                return len(self._ducked)

            try:
                with pulsectl.Pulse(f"{self._name}-ducker") as pulse:
                    streams = [
                        stream
                        for stream in pulse.sink_input_list()
                        if not stream.corked
                        and stream.index not in taken
                        and not self._is_excluded(stream, extra)
                    ]
                    for stream in streams:
                        values = list(stream.volume.values)
                        self._ducked[stream.index] = DuckedStream(
                            self._identity(stream),
                            values,
                            [v * self._factor for v in values],
                        )
                    self._record()
                    for stream in streams:
                        pulse.volume_set(
                            stream,
                            pulsectl.PulseVolumeInfo(
                                self._ducked[stream.index].applied
                            ),
                        )
            except pulsectl.PulseError as e:
                # Whatever was lowered before the failure still needs putting
                # back, so the snapshot outlives the error rather than being
                # dropped with those streams left quiet.
                log.warning("duck failed: %s", e)

            count = len(self._ducked)
            log.debug("ducked %d stream(s) by %.2fx", count, self._factor)

            return count

    def settle(
        self, entries: dict[int, DuckedStream], owed: dict[str, DuckedStream]
    ) -> dict[str, DuckedStream]:
        """Put `entries` back, and lift `owed` apps; returns the apps still owed.

        A stream whose volume no longer matches what `duck` set is left alone:
        its app moved it in the meantime, and Spotify does exactly that at every
        track change, so writing the snapshot back would replace a value the app
        chose with a stale one.

        A stream that is not in `entries` but belongs to an app that is - one
        that opened while ducked and took the remembered level, or one opened
        since an owed app's stream ended - is lifted when it sits at that
        app's ducked level. An app stays owed only while none of its streams
        is playing; any live stream means WirePlumber's memory of it has moved
        on from the ducked level, or is about to."""
        by_app = owed | {entry.app: entry for entry in entries.values() if entry.app}
        if not by_app:
            return {}

        seen: set[str] = set()
        restored = 0
        try:
            with pulsectl.Pulse(f"{self._name}-ducker") as pulse:
                for stream in pulse.sink_input_list():
                    identity = self._identity(stream)
                    entry = entries.get(stream.index)
                    # Indices get recycled, so a stream that ended while
                    # ducked can hand its number to an unrelated new one.
                    if entry is None or entry.identity != identity:
                        entry = by_app.get(identity[2] or identity[1])
                        if entry is None:
                            continue
                    if entry.app:
                        seen.add(entry.app)
                    if not entry.is_applied(list(stream.volume.values)):
                        log.debug(
                            "stream %d moved since duck; leaving it", stream.index
                        )
                        continue
                    pulse.volume_set(stream, pulsectl.PulseVolumeInfo(entry.values))
                    restored += 1
        except pulsectl.PulseError as e:
            log.warning("restore failed: %s", e)
            return by_app

        log.debug("restored %d stream(s)", restored)

        return {app: entry for app, entry in by_app.items() if app not in seen}

    def restore(self) -> dict[str, DuckedStream]:
        """Put every ducked stream back at its original volume; returns the apps owed.

        A no-op when nothing is ducked, so callers can put it in a `finally`
        without tracking whether `duck` ever ran."""
        with self._lock:
            if not self._ducked:
                return {}

            owed = self.settle(self._ducked, {})
            self._ducked.clear()
            self._record()

            return owed

class MediaPauser:
    """Pauses playing MPRIS players through playerctl, and resumes only those.

    playerctl rather than the bus directly: it is already installed, and it
    keeps this module free of a D-Bus binding that every dotlib consumer would
    otherwise have to carry."""

    # Every playerctl call sits on the path to speaking, so a wedged player
    # must never stall an utterance.
    def __init__(self, *, timeout: float = 2.0, journal: SuppressJournal | None = None):
        self._timeout = timeout
        self._journal = journal
        self._paused: list[str] = []
        self._lock = threading.Lock()

    def _record(self) -> None:
        if self._journal is not None:
            self._journal.players = list(self._paused)
            self._journal.save()

    def _playerctl(self, *args: str) -> str | None:
        """Run playerctl; None on any failure, stripped stdout otherwise."""
        cmd = ["playerctl", *args]
        log.debug("spawn: %s", " ".join(cmd))
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            log.warning("playerctl failed: %s", e)
            return None

        if proc.returncode != 0:
            log.debug("playerctl exit=%d: %s", proc.returncode, proc.stderr.strip())
            return None

        return proc.stdout.strip()

    @property
    def is_paused(self) -> bool:
        with self._lock:
            return bool(self._paused)

    def pause(self) -> list[str]:
        """Pause every MPRIS player that is playing; returns their names."""
        if is_headless():
            log.debug("headless: skipping pause")
            return []

        with self._lock:
            if self._paused:
                return list(self._paused)

            listing = self._playerctl("-a", "-f", "{{playerName}}|{{status}}", "status")
            for line in (listing or "").splitlines():
                name, _, status = line.partition("|")
                if status.strip() != "Playing":
                    continue
                if self._playerctl("-p", name, "pause") is None:
                    continue
                self._paused.append(name)
                self._record()

            log.debug("paused %d player(s): %s", len(self._paused), self._paused)

            return list(self._paused)

    def play(self, names: list[str], *, paused_only: bool = False) -> int:
        """Resume `names` that are still listed; returns how many.

        One that quit while we were speaking never asked to play again — though
        a player that quit and restarted under the same name is
        indistinguishable here, which the bus could tell apart by unique owner
        and playerctl cannot. `paused_only` also skips a player that is not
        sitting paused, for replaying a dead run's record, where the player
        may have been stopped or started by hand since."""
        listing = self._playerctl("-a", "-f", "{{playerName}}|{{status}}", "status")
        status = dict(line.partition("|")[::2] for line in (listing or "").splitlines())
        resumed = 0
        # Reverse order, so the last player paused is the first back.
        for name in reversed(names):
            if name not in status:
                log.debug("player %s is gone; not resuming it", name)
                continue
            if paused_only and status[name].strip() != "Paused":
                log.debug("player %s is %s; not resuming it", name, status[name])
                continue
            if self._playerctl("-p", name, "play") is not None:
                resumed += 1
        log.debug("resumed %d player(s)", resumed)

        return resumed

    def resume(self) -> int:
        """Resume the players `pause` stopped; returns how many. Idempotent."""
        with self._lock:
            if not self._paused:
                return 0

            resumed = self.play(self._paused)
            self._paused.clear()
            self._record()

            return resumed

class PlaybackSuppressor:
    """Quiets other audio for as long as a caller needs the room.

    Pausing runs first, and the players it stopped are then kept out of the duck
    sweep by name. Waiting for their streams to cork instead would mean guessing
    a delay: Spotify takes ~265ms, so a shorter one ducks it anyway and a longer
    one is dead time before every utterance. Name matching costs nothing and is
    exact where it applies; a player whose playerctl name does not match its
    PulseAudio `application.name` is merely ducked as well as paused, which the
    restore undoes.

    Both ends first undo whatever earlier runs left behind - see `recover` -
    so a run that was killed outright is repaired by the next one."""

    def __init__(
        self,
        *,
        duck: bool = True,
        factor: float = DEFAULT_DUCK_FACTOR,
        pause: bool = True,
        name: str = "dotlib",
        exclude: Collection[str] = (),
    ):
        self._duck = duck
        self._pause = pause
        self._journal = SuppressJournal()
        self._ducker = Ducker(factor, name=name, exclude=exclude, journal=self._journal)
        self._pauser = MediaPauser(journal=self._journal)

    @property
    def is_active(self) -> bool:
        return self._ducker.is_ducked or self._pauser.is_paused

    def recover(self, owed: dict[str, DuckedStream] | None = None) -> None:
        """Undo what dead runs left behind, and lift every app still owed.

        `owed` adds apps this run's own restore could not reach. A pulse
        failure here turns every replayed stream into an owed app, so a dead
        run's record can be dropped either way."""
        if is_headless():
            return

        with self._journal.locked():
            dead = [
                (path, data) for path, data, live in self._journal.others() if not live
            ]
            owed = self._journal.owed() | (owed or {})
            streams: dict[int, DuckedStream] = {}
            players: list[str] = []
            for path, data in dead:
                log.info("recovering suppression left by %s", path.name)
                entries = {
                    int(index): DuckedStream.load(entry)
                    for index, entry in data.get("streams", {}).items()
                }
                # A past boot's streams and players are gone, but WirePlumber
                # kept the ducked level of every one of them.
                if data.get("boot") != self._journal.boot:
                    owed |= {
                        entry.app: entry for entry in entries.values() if entry.app
                    }
                    continue
                streams |= entries
                players += data.get("players", [])

            remaining = self._ducker.settle(streams, owed)
            if players:
                self._pauser.play(players, paused_only=True)
            self._journal.save_owed(remaining)
            for path, _ in dead:
                path.unlink(missing_ok=True)
            if remaining:
                log.debug("still owed: %s", ", ".join(remaining))

    def suppress(self) -> None:
        self.recover()
        paused = self._pauser.pause() if self._pause else []
        if self._duck:
            # playerctl qualifies a name per instance (`firefox.instance_1_7`);
            # PulseAudio knows only the application, so compare the stem.
            self._ducker.duck(exclude=[name.split(".")[0] for name in paused])

    def restore(self) -> None:
        owed = self._ducker.restore()
        self._pauser.resume()
        self.recover(owed)
