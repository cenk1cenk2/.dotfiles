"""Duck other applications' playback while a script makes noise of its own.

Ducking works on sink inputs — the per-application streams — never on the
sink itself. A sink's volume is the output device's master volume, which
desktop shells (swayosd, swaync, ...) watch and answer with a volume OSD on
every utterance, and a crash while ducked would leave the speakers wrong
until someone noticed.

Streams are snapshotted once, at duck time, so anything that starts while
ducked plays at full volume. Catching late arrivals needs a sink-input event
subscription; until then that is the price of leaving master volume alone."""

from __future__ import annotations

import logging
import threading
from collections.abc import Collection
from dataclasses import dataclass

import pulsectl

from .desktop import is_headless

DEFAULT_DUCK_FACTOR = 0.3

log = logging.getLogger(__name__)

@dataclass(frozen=True)
class DuckedStream:
    """A stream's volume as it was, plus enough to know it is still that stream."""

    identity: tuple[str | None, ...]
    values: list[float]

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

    def _is_excluded(self, stream: pulsectl.PulseSinkInputInfo) -> bool:
        """True for a stream the caller named as one of its own.

        The stream-restore database is keyed on the application name, so
        ducking someone else's `ffplay` and then spawning our own hands the
        new stream the ducked volume — snapshotting before the spawn keeps it
        out of `restore`, but not out of that lookup. PipeWire-native clients
        set only `application.name`, so both keys have to be checked."""
        props = stream.proplist
        binary = (props.get("application.process.binary") or "").lower()
        app = (props.get("application.name") or "").lower()

        return binary in self._exclude or app in self._exclude

    @property
    def is_ducked(self) -> bool:
        with self._lock:
            return bool(self._ducked)

    def duck(self) -> int:
        """Scale every live playback stream by `factor`; returns how many.

        Corked streams are skipped. A paused stream is worth nothing to duck
        and costs something: if it goes away before `restore` runs, the
        stream-restore database keeps the ducked volume against that app and
        hands it back to the next stream it opens."""
        if is_headless():
            log.debug("headless: skipping duck")
            return 0

        with self._lock:
            if self._ducked:
                return len(self._ducked)

            try:
                with pulsectl.Pulse(f"{self._name}-ducker") as pulse:
                    for stream in pulse.sink_input_list():
                        if stream.corked or self._is_excluded(stream):
                            continue
                        values = list(stream.volume.values)
                        self._ducked[stream.index] = DuckedStream(
                            self._identity(stream), values
                        )
                        pulse.volume_set(
                            stream,
                            pulsectl.PulseVolumeInfo(
                                [v * self._factor for v in values]
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

    def restore(self) -> int:
        """Put every ducked stream back at its original volume; returns how many.

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
                        pulse.volume_set(stream, pulsectl.PulseVolumeInfo(entry.values))
                        restored += 1
            except pulsectl.PulseError as e:
                log.warning("restore failed: %s", e)

            self._ducked.clear()
            log.debug("restored %d stream(s)", restored)

            return restored
