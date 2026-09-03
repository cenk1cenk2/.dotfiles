"""Telling the user something happened, by popup or by sound.

Both halves answer the same question - how does a script get the user's
attention - and both go quiet when nothing is there to notice. Manipulating
audio that belongs to other applications is a different job and lives in
`audio`."""

from __future__ import annotations

import logging
import math
import struct
import subprocess
import sys
import time
from enum import StrEnum

from .desktop import is_headless

log = logging.getLogger(__name__)


class NotifyChannel(StrEnum):
    """Where a message goes.

    The card is the default because it replaces itself in place, so a job that
    reports several times leaves one row rather than a stack of popups. The
    desktop notification is for something worth surviving the glance away."""

    OSD = "osd"
    DESKTOP = "desktop"


class OsdIcon(StrEnum):
    """Freedesktop icon names, so the card says what kind of work this is."""

    MIC = "audio-input-microphone"
    SPEAKER = "audio-speakers"
    THINKING = "system-run"
    DONE = "object-select"
    ERROR = "dialog-error"


class Notification:
    """Telling the user how a piece of work is going.

    Title and icons belong to the script rather than the message, so they are
    bound once and every call after that carries only what changed. The same
    message text serves either channel; only the icon differs, because
    notify-send wants a path and swayosd wants a freedesktop name.

    Both channels go quiet when headless — there is nobody to tell."""

    BUS = "org.erikreider.swayosd-server"
    PATH = "/org/erikreider/swayosd"
    INTERFACE = "org.erikreider.swayosd"

    # swayosd hides a card when its timer expires and offers no explicit hide,
    # so a card is held open by re-firing inside this window and let go by
    # firing once with a short one.
    HOLD_MS = 2000
    DISMISS_MS = 200
    # A wedged call must never delay the work it is describing.
    TIMEOUT = 2.0

    def __init__(
        self,
        title: str,
        icon: str = "",
        osd_icon: OsdIcon | None = None,
        channel: NotifyChannel = NotifyChannel.OSD,
    ):
        self.title = title
        self.icon = icon
        self.osd_icon = osd_icon
        self.channel = channel
        self._started = 0.0

    # ── the two channels ──────────────────────────────────────────

    def _desktop(self, message: str, timeout: int | None) -> None:
        cmd = ["notify-send", self.title, message, "-i", self.icon]
        if timeout:
            cmd.extend(["-t", str(timeout)])
        log.debug("spawn: %s", " ".join(cmd))
        subprocess.run(cmd, check=False, stdout=sys.stderr, stderr=sys.stderr)

    def _card(self, message: str, duration_ms: int, icon: OsdIcon | None) -> None:
        """One `HandleAction` on swayosd's bus.

        The bus rather than `swayosd-client`, which is a thin wrapper over this
        single call: a card that ticks a clock would otherwise cost a process a
        second. The interface is undocumented and was read off `dbus-monitor`
        while the client ran."""
        options = [("DURATION", str(duration_ms))]
        chosen = icon or self.osd_icon
        if chosen:
            options.insert(0, ("CUSTOM-ICON", chosen.value))
        text = f"{self.title}  {message}" if self.title else message
        self._call("CUSTOM-MESSAGE", text, options)

    def _call(self, action: str, value: str, options: list[tuple[str, str]]) -> None:
        flat: list[str] = []
        for key, option in options:
            flat += [key, option]
        cmd = [
            "busctl", "--user", "call", self.BUS, self.PATH, self.INTERFACE,
            "HandleAction", "ssa(ss)", action, value, str(len(options)), *flat,
        ]
        log.debug("spawn: %s", " ".join(cmd))
        try:
            subprocess.run(cmd, capture_output=True, timeout=self.TIMEOUT, check=False)
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            # A card is never the point of the work it describes.
            log.warning("osd call failed: %s", e)

    # ── what callers use ──────────────────────────────────────────

    def send(
        self,
        message: str,
        timeout: int | None = None,
        *,
        icon: OsdIcon | None = None,
        channel: NotifyChannel | None = None,
    ) -> None:
        """Say something, on this notification's channel or an override."""
        if is_headless():
            log.debug("headless: dropping %r", message)
            return

        if (channel or self.channel) is NotifyChannel.DESKTOP:
            self._desktop(message, timeout)
            return

        if not self._started:
            self._started = time.monotonic()
        self._card(message, timeout or self.HOLD_MS, icon)

    def elapsed(
        self,
        message: str = "",
        *,
        icon: OsdIcon | None = None,
        level: float | None = None,
    ) -> None:
        """Say it again with the time since this notification first went up.

        The clock is ours: swayosd renders the string it is handed and has no
        notion of one running. A `level` draws its progress bar underneath,
        which is a real bar rather than block characters pretending to be one."""
        seconds = int(time.monotonic() - self._started) if self._started else 0
        stamp = f"{seconds // 60:d}:{seconds % 60:02d}"
        self.send(f"{stamp}  {message}".rstrip(), icon=icon)
        if level is not None and not is_headless():
            self._bar(level)

    def _bar(self, level: float) -> None:
        """Draw the progress bar. A second action — swayosd takes them apart."""
        self._call(
            "CUSTOM-PROGRESS",
            f"{min(max(level, 0.0), 1.0):.2f}",
            [("DURATION", str(self.HOLD_MS))],
        )

    def dismiss(self, message: str = "", *, icon: OsdIcon | None = None) -> None:
        """Let it go, after a beat if there is a parting word."""
        self._started = 0.0
        if message and not is_headless():
            if self.channel is NotifyChannel.DESKTOP:
                self._desktop(message, None)
            else:
                self._card(message, self.DISMISS_MS, icon)


class ChimeDirection(StrEnum):
    """Rising for something starting, falling for something finished, level for
    something that happened along the way and left the job still running."""

    UP = "up"
    DOWN = "down"
    FLAT = "flat"


class Chime:
    """A short alert tone, generated rather than shipped.

    Root and fifth, an interval that reads as neither happy nor sad - the
    pairing UI sound design reaches for when a tone has to be neutral. C4 and G4
    sit low enough not to pierce and well inside the 125Hz-5kHz band earcons are
    legible in. Direction is the only thing separating the three, which is what
    the earcon guidelines call a relative judgement: the one job pitch alone is
    reliably good at."""

    # Partials of a struck marimba bar, not a sine. The earcon research is blunt
    # about this - real instrument timbres were recognised significantly better
    # than sinusoidal tones, and a bar rings at roughly 1:4:10 rather than the
    # harmonic 1:2:3 a synthesised "bell" reaches for by default.
    # (frequency ratio, amplitude, decay multiplier)
    PARTIALS = ((1.0, 1.0, 1.0), (4.0, 0.30, 0.30), (10.0, 0.10, 0.12))
    NOTES_HZ = (261.63, 392.00)
    # Gap between strikes. Below the 0.0825s floor the guidelines set for a
    # complex earcon, which they explicitly relax for one of two notes.
    NOTE_SECONDS = 0.07
    DECAY_SECONDS = 0.17
    # First note louder and last note longer, so the pair lands as one rhythmic
    # unit rather than two loose beeps.
    ACCENT = 1.15
    TAIL = 1.3
    # Fast, but not instantaneous - a zero-length attack is a click.
    ATTACK_SECONDS = 0.002
    # Silence before the first strike, and after the last. PipeWire suspends an
    # idle sink, and resuming one takes long enough that a tone this short can
    # be half over before the device is producing sound - so the opening note
    # simply is not there. The lead-in gives the sink something to wake up on;
    # the tail gives the buffer time to drain before `ffplay -autoexit` quits at
    # input EOF. Neither is audible, and neither lengthens the tone itself.
    LEAD_SECONDS = 0.12
    PAD_SECONDS = 0.06
    # Peak level after normalisation. Quiet relative to speech: a level filter
    # sees the pair together, and a loud chime would drag the voice down with it.
    GAIN = 0.22
    # Kokoro's rate, and the one the TTS player is already configured for. A
    # standalone chime has no stream to match, so it just needs to be sane.
    SAMPLE_RATE = 24000
    # A tone this short either plays at once or not at all; a stuck player must
    # not hold up whatever the caller was about to do next.
    TIMEOUT = 5.0

    def __init__(
        self,
        direction: ChimeDirection = ChimeDirection.UP,
        sample_rate: int = SAMPLE_RATE,
    ):
        self.direction = direction
        self.sample_rate = sample_rate

    def _notes(self) -> tuple[float, ...]:
        if self.direction is ChimeDirection.DOWN:
            return tuple(reversed(self.NOTES_HZ))
        if self.direction is ChimeDirection.FLAT:
            # The root struck twice, so the rhythm and register match its
            # siblings exactly and only the contour tells them apart.
            return (self.NOTES_HZ[0],) * len(self.NOTES_HZ)

        return self.NOTES_HZ

    def pcm(self) -> bytes:
        """The tone as raw s16le mono.

        Prepending the result to a synthesis stream keeps playback to a single
        process: no second spawn to race the first, no gap, and it inherits
        whatever the player is already doing about ducking and loudness.

        Empty when headless, so a caller can splice it in unconditionally and
        get silence rather than a tone nobody is there to hear."""
        if is_headless():
            log.debug("headless: no chime")
            return b""

        notes = self._notes()
        span = self.NOTE_SECONDS * (len(notes) - 1) + self.DECAY_SECONDS * self.TAIL
        lead = int(self.sample_rate * self.LEAD_SECONDS)
        total = lead + int(self.sample_rate * (span + self.PAD_SECONDS))
        attack = max(1, int(self.sample_rate * self.ATTACK_SECONDS))
        last = len(notes) - 1
        mix = [0.0] * total

        for note, frequency in enumerate(notes):
            start = lead + int(self.sample_rate * self.NOTE_SECONDS * note)
            accent = self.ACCENT if note == 0 else 1.0
            decay = self.DECAY_SECONDS * (self.TAIL if note == last else 1.0)
            for ratio, amplitude, scale in self.PARTIALS:
                tau = decay * scale
                omega = 2.0 * math.pi * frequency * ratio / self.sample_rate
                for i in range(total - start):
                    # Test the decay alone, never the attack: the attack ramp
                    # opens at zero, so a combined test reads the first sample
                    # as a decayed tail and breaks before the note has begun.
                    decayed = math.exp(-i / self.sample_rate / tau)
                    # The tail is inaudible long before the buffer ends;
                    # stopping here keeps this from being a slow Python loop.
                    if decayed < 1e-4:
                        break
                    envelope = min(1.0, i / attack) * decayed
                    mix[start + i] += accent * amplitude * envelope * math.sin(
                        omega * i
                    )

        # Normalise to the peak rather than trusting the partials to sum to
        # something predictable, so the level is the same whatever they are set
        # to.
        scale = self.GAIN / max(max(abs(v) for v in mix), 1e-9) * 32767

        return struct.pack(f"<{total}h", *(int(v * scale) for v in mix))

    def play(self) -> None:
        """Play the tone on its own, for a caller with no stream to ride.

        Blocks for the length of the tone rather than detaching: it is a third
        of a second, and a fire-and-forget child would linger as a zombie in any
        caller that outlives it."""
        pcm = self.pcm()
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
            str(self.sample_rate),
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
                timeout=self.TIMEOUT,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            log.warning("chime failed: %s", e)
