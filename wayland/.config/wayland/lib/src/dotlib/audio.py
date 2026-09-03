"""Quiet other applications' playback while a script makes noise of its own.

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
subscription; until then that is the price of leaving master volume alone."""

from __future__ import annotations

import logging
import math
import struct
import subprocess
import threading
from collections.abc import Collection
from dataclasses import dataclass
from enum import StrEnum

import pulsectl

from .desktop import is_headless

DEFAULT_DUCK_FACTOR = 0.3

# Every playerctl call sits on the path to speaking, so a wedged player must
# never stall an utterance.
PLAYERCTL_TIMEOUT = 2.0

# Root and fifth, an interval that reads as neither happy nor sad - the pairing
# UI sound design reaches for when a tone has to be neutral. C4 and G4 sit low
# enough not to pierce and well inside the 125Hz-5kHz band earcons are legible
# in. Direction is the only thing that separates start from end, which is what
# the earcon guidelines call a relative judgement: the one job pitch alone is
# reliably good at.
CHIME_NOTES_HZ = (261.63, 392.00)
# Partials of a struck marimba bar, not a sine. The earcon research is blunt
# about this - real instrument timbres were recognised significantly better
# than sinusoidal tones, and a bar rings at roughly 1:4:10 rather than the
# harmonic 1:2:3 a synthesised "bell" reaches for by default.
# (frequency ratio, amplitude, decay multiplier)
CHIME_PARTIALS = ((1.0, 1.0, 1.0), (4.0, 0.30, 0.30), (10.0, 0.10, 0.12))
# Gap between strikes. Below the 0.0825s floor the guidelines set for a complex
# earcon, which they explicitly relax for one of two notes.
CHIME_NOTE_SECONDS = 0.07
CHIME_DECAY_SECONDS = 0.17
# First note louder and last note longer, so the pair lands as one rhythmic
# unit rather than two loose beeps.
CHIME_ACCENT = 1.15
CHIME_TAIL = 1.3
# Fast, but not instantaneous - a zero-length attack is a click.
CHIME_ATTACK_SECONDS = 0.002
# Silence before the first strike, and after the last. PipeWire suspends an
# idle sink, and resuming one takes long enough that a tone this short can be
# half over before the device is producing sound - so the opening note simply
# is not there. The lead-in gives the sink something to wake up on; the tail
# gives the buffer time to drain before `ffplay -autoexit` quits at input EOF.
# Neither is audible, and neither lengthens the tone itself.
CHIME_LEAD_SECONDS = 0.12
CHIME_PAD_SECONDS = 0.06
# Peak level after normalisation. Quiet relative to the speech: the level filter
# sees the pair together, and a loud chime would drag the voice down with it.
CHIME_GAIN = 0.22
# Kokoro's rate, and the one the TTS player is already configured for. A
# standalone chime has no stream to match, so it just needs to be sane.
CHIME_SAMPLE_RATE = 24000
# A tone this short either plays at once or not at all; a stuck player must not
# hold up whatever the caller was about to do next.
CHIME_TIMEOUT = 5.0

# Volumes come back as floats; PulseAudio rounds through integer units, so an
# exact comparison would reject a stream nobody touched.
VOLUME_EPSILON = 0.01

log = logging.getLogger(__name__)

class ChimeDirection(StrEnum):
    """Rising for something starting, falling for something finished, level for
    something that happened along the way and left the job still running."""

    UP = "up"
    DOWN = "down"
    FLAT = "flat"

@dataclass(frozen=True)
class DuckedStream:
    """A stream's volume as it was, plus enough to know nothing else moved it."""

    identity: tuple[str | None, ...]
    values: list[float]
    applied: list[float]

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
    ):
        self._factor = min(max(factor, 0.0), 1.0)
        self._name = name
        self._exclude = {value.lower() for value in exclude}
        self._ducked: dict[int, DuckedStream] = {}
        self._lock = threading.Lock()

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
        hands it back to the next stream it opens."""
        if is_headless():
            log.debug("headless: skipping duck")
            return 0

        extra = {value.lower() for value in exclude}
        with self._lock:
            if self._ducked:
                return len(self._ducked)

            try:
                with pulsectl.Pulse(f"{self._name}-ducker") as pulse:
                    for stream in pulse.sink_input_list():
                        if stream.corked or self._is_excluded(stream, extra):
                            continue
                        values = list(stream.volume.values)
                        applied = [v * self._factor for v in values]
                        self._ducked[stream.index] = DuckedStream(
                            self._identity(stream), values, applied
                        )
                        pulse.volume_set(stream, pulsectl.PulseVolumeInfo(applied))
            except pulsectl.PulseError as e:
                # Whatever was lowered before the failure still needs putting
                # back, so the snapshot outlives the error rather than being
                # dropped with those streams left quiet.
                log.warning("duck failed: %s", e)

            count = len(self._ducked)
            log.debug("ducked %d stream(s) by %.2fx", count, self._factor)

            return count

    def restore(self) -> int:
        """Put every ducked stream back at its original volume; returns how many.

        A stream whose volume no longer matches what `duck` set is left alone:
        its app moved it in the meantime, and Spotify does exactly that at every
        track change, so writing the snapshot back would replace a value the app
        chose with a stale one.

        A no-op when nothing is ducked, so callers can put it in a `finally`
        without tracking whether `duck` ever ran."""
        with self._lock:
            if not self._ducked:
                return 0

            restored = 0
            try:
                with pulsectl.Pulse(f"{self._name}-ducker") as pulse:
                    for stream in pulse.sink_input_list():
                        entry = self._ducked.get(stream.index)
                        # Indices get recycled, so a stream that ended while
                        # ducked can hand its number to an unrelated new one.
                        if entry is None or entry.identity != self._identity(stream):
                            continue
                        current = list(stream.volume.values)
                        if len(current) != len(entry.applied) or any(
                            abs(now - was) > VOLUME_EPSILON
                            for now, was in zip(current, entry.applied)
                        ):
                            log.debug("stream %d moved since duck; leaving it", stream.index)
                            continue
                        pulse.volume_set(stream, pulsectl.PulseVolumeInfo(entry.values))
                        restored += 1
            except pulsectl.PulseError as e:
                log.warning("restore failed: %s", e)

            self._ducked.clear()
            log.debug("restored %d stream(s)", restored)

            return restored

class MediaPauser:
    """Pauses playing MPRIS players through playerctl, and resumes only those.

    playerctl rather than the bus directly: it is already installed, and it
    keeps this module free of a D-Bus binding that every dotlib consumer would
    otherwise have to carry."""

    def __init__(self, *, timeout: float = PLAYERCTL_TIMEOUT):
        self._timeout = timeout
        self._paused: list[str] = []
        self._lock = threading.Lock()

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

            log.debug("paused %d player(s): %s", len(self._paused), self._paused)

            return list(self._paused)

    def resume(self) -> int:
        """Resume the players `pause` stopped; returns how many. Idempotent.

        Only players still listed are resumed. One that quit while we were
        speaking never asked to play again — though a player that quit and
        restarted under the same name is indistinguishable here, which the bus
        could tell apart by unique owner and playerctl cannot."""
        with self._lock:
            if not self._paused:
                return 0

            live = (self._playerctl("-l") or "").splitlines()
            resumed = 0
            # Reverse order, so the last player paused is the first back.
            for name in reversed(self._paused):
                if name not in live:
                    log.debug("player %s is gone; not resuming it", name)
                    continue
                if self._playerctl("-p", name, "play") is not None:
                    resumed += 1

            self._paused.clear()
            log.debug("resumed %d player(s)", resumed)

            return resumed

class PlaybackSuppressor:
    """Quiets other audio for as long as a caller needs the room.

    Pausing runs first, and the players it stopped are then kept out of the duck
    sweep by name. Waiting for their streams to cork instead would mean guessing
    a delay: Spotify takes ~265ms, so a shorter one ducks it anyway and a longer
    one is dead time before every utterance. Name matching costs nothing and is
    exact where it applies; a player whose playerctl name does not match its
    PulseAudio `application.name` is merely ducked as well as paused, which the
    restore undoes."""

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
        self._ducker = Ducker(factor, name=name, exclude=exclude)
        self._pauser = MediaPauser()

    @property
    def is_active(self) -> bool:
        return self._ducker.is_ducked or self._pauser.is_paused

    def suppress(self) -> None:
        paused = self._pauser.pause() if self._pause else []
        if self._duck:
            # playerctl qualifies a name per instance (`firefox.instance_1_7`);
            # PulseAudio knows only the application, so compare the stem.
            self._ducker.duck(exclude=[name.split(".")[0] for name in paused])

    def restore(self) -> None:
        self._ducker.restore()
        self._pauser.resume()

def chime_pcm(
    direction: ChimeDirection = ChimeDirection.UP,
    sample_rate: int = CHIME_SAMPLE_RATE,
) -> bytes:
    """The alert tone as raw s16le mono, generated rather than shipped.

    Rising means something is starting, falling means it has finished, level
    means something happened mid-flight and the job is still running. All three
    are the same notes over the same rhythm, so they read as one family rather
    than three unrelated sounds.

    Prepending the result to a synthesis stream keeps playback to a single
    process: no second spawn to race the first, no gap, and it inherits whatever
    the player is already doing about ducking and loudness.

    Empty when headless, so a caller can splice it in unconditionally and get
    silence rather than a tone nobody is there to hear."""
    if is_headless():
        log.debug("headless: no chime")
        return b""

    notes = CHIME_NOTES_HZ
    if direction is ChimeDirection.DOWN:
        notes = tuple(reversed(notes))
    elif direction is ChimeDirection.FLAT:
        # The root struck twice. Same timbre, same rhythm, same register as its
        # siblings, so only the contour separates the three - which is the one
        # job the earcon guidelines say pitch alone does reliably.
        notes = (notes[0],) * len(notes)

    span = CHIME_NOTE_SECONDS * (len(notes) - 1) + CHIME_DECAY_SECONDS * CHIME_TAIL
    lead = int(sample_rate * CHIME_LEAD_SECONDS)
    total = lead + int(sample_rate * (span + CHIME_PAD_SECONDS))
    attack = max(1, int(sample_rate * CHIME_ATTACK_SECONDS))
    last = len(notes) - 1
    mix = [0.0] * total

    for note, frequency in enumerate(notes):
        start = lead + int(sample_rate * CHIME_NOTE_SECONDS * note)
        accent = CHIME_ACCENT if note == 0 else 1.0
        decay = CHIME_DECAY_SECONDS * (CHIME_TAIL if note == last else 1.0)
        for ratio, amplitude, scale in CHIME_PARTIALS:
            tau = decay * scale
            omega = 2.0 * math.pi * frequency * ratio / sample_rate
            for i in range(total - start):
                # Test the decay alone, never the attack: the attack ramp opens
                # at zero, so a combined test reads the first sample as a
                # decayed tail and breaks before the note has begun.
                decayed = math.exp(-i / sample_rate / tau)
                # The tail is inaudible long before the buffer ends; stopping
                # here is what keeps this from being a slow loop in pure Python.
                if decayed < 1e-4:
                    break
                envelope = min(1.0, i / attack) * decayed
                mix[start + i] += accent * amplitude * envelope * math.sin(omega * i)

    # Normalise to the peak rather than trusting the partials to sum to
    # something predictable, so the level is the same whatever they are set to.
    scale = CHIME_GAIN / max(max(abs(v) for v in mix), 1e-9) * 32767

    return struct.pack(f"<{total}h", *(int(v * scale) for v in mix))


def play_chime(
    direction: ChimeDirection = ChimeDirection.DOWN,
    sample_rate: int = CHIME_SAMPLE_RATE,
) -> None:
    """Play the alert tone on its own, for a caller with no stream to ride.

    Blocks for the length of the tone rather than detaching: it is a third of a
    second, and a fire-and-forget child would linger as a zombie in any caller
    that outlives it."""
    pcm = chime_pcm(direction, sample_rate)
    if not pcm:
        return

    cmd = [
        "ffplay",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nodisp",
        "-autoexit",
        "-f",
        "s16le",
        "-ar",
        str(sample_rate),
        "-ch_layout",
        "mono",
        "-i",
        "pipe:0",
    ]
    log.debug("spawn: %s", " ".join(cmd))
    try:
        subprocess.run(
            cmd,
            input=pcm,
            capture_output=True,
            timeout=CHIME_TIMEOUT,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        log.warning("chime failed: %s", e)
