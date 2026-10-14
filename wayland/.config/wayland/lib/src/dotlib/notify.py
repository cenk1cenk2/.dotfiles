"""Telling the user something happened, by popup, by sound, or by bell.

All three answer the same question - how does a script get the user's
attention - and all go quiet when nothing is there to notice. Manipulating
audio that belongs to other applications is a different job and lives in
`audio`."""

from __future__ import annotations

import logging
import math
import os
import struct
import subprocess
import sys
import time
from collections.abc import Iterator, Sequence
from enum import StrEnum
from typing import ClassVar

from .desktop import is_headless
from .overlay import Overlay, OverlayPosition, OverlaySlot

log = logging.getLogger(__name__)


class NotifyChannel(StrEnum):
    """Where a message goes.

    The card is the default because it replaces itself in place, so a job that
    reports several times leaves one row rather than a stack of popups. The
    desktop notification is for something worth surviving the glance away."""

    OSD = "osd"
    DESKTOP = "desktop"


class Urgency(StrEnum):
    """notify-send urgency. Critical holds the popup until dismissed, which is
    the difference between a heads-up and something that must not be missed."""

    LOW = "low"
    NORMAL = "normal"
    CRITICAL = "critical"


class OsdIcon(StrEnum):
    """Freedesktop icon names, so the card says what kind of work this is."""

    MIC = "audio-input-microphone"
    # Not `audio-speakers`: Tela ships that one with a NaN arc, and GTK 4.22
    # spins on it, leaking gigabytes a second until the process drawing it is
    # OOM-killed.
    SPEAKER = "audio-volume-high"
    THINKING = "system-run"
    WRITING = "document-edit-symbolic"
    DONE = "object-select"
    ERROR = "dialog-error"


class Notification:
    """Telling the user how a piece of work is going.

    Title and icons belong to the script rather than the message, so they are
    bound once and every call after that carries only what changed. The same
    message text serves either channel; only the icon differs, because
    the two resolve a freedesktop name against different icon sets.

    Both channels go quiet when headless — there is nobody to tell."""

    # The card hides itself when this runs out, so a caller that stops
    # refreshing never leaves a stale card behind. Long enough to span the gap
    # between one phase of a job and the next, so the card stays up rather
    # than blinking out between them; `dismiss` and `hide` take it down at once.
    HOLD_MS = 3000
    # How often a held card is redrawn. Short enough that a level meter reads
    # as one, and well inside the hold so the card never blinks between.
    TICK_SECONDS = 0.2
    TICK_HOLD_MS = 3000
    # How long a parting word stays, long enough to be read.
    DISMISS_MS = 2000
    # How much of a growing text `tail` keeps.
    CARD_CHARS = 128
    # A wedged call must never delay the work it is describing.
    TIMEOUT = 2.0

    # One connection per process, however many notifications it holds.
    _overlay: ClassVar[Overlay] = Overlay()

    def __init__(
        self,
        title: str,
        icon: str = "",
        osd_icon: OsdIcon | None = None,
        channel: NotifyChannel = NotifyChannel.OSD,
        position: OverlayPosition = OverlayPosition.BOTTOM_RIGHT,
    ):
        self.title = title
        self.position = position
        self.icon = icon
        self.osd_icon = osd_icon
        self.channel = channel
        self._started = 0.0
        self._stopped = 0.0

    # ── the two channels ──────────────────────────────────────────

    def _desktop(
        self,
        message: str,
        timeout: int | None,
        urgency: Urgency | None = None,
        actions: Sequence[tuple[str, str]] | None = None,
    ) -> str | None:
        cmd = ["notify-send", self.title, message, "-i", self.icon]
        if timeout:
            cmd.extend(["-t", str(timeout)])
        if urgency:
            cmd.extend(["-u", urgency.value])
        for name, label in actions or ():
            cmd.extend(["-A", f"{name}={label}"])
        log.debug("spawn: %s", " ".join(cmd))
        if actions:
            # notify-send stays up until the popup is activated or expires,
            # then prints the chosen action's name — that is the return value,
            # so a caller asking for actions accepts the wait.
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
            return proc.stdout.strip() or None
        subprocess.run(cmd, check=False, stdout=sys.stderr, stderr=sys.stderr)
        return None

    def _card(
        self,
        timeout_ms: int,
        icon: OsdIcon | None,
        *,
        status: str = "",
        clock: str | None = None,
        level: float | None = None,
        text: str = "",
        extract: bool = False,
    ) -> None:
        """Every call carries the whole card; a field left out is drawn empty."""
        chosen = icon or self.osd_icon
        self._overlay.show(
            OverlaySlot.CARD,
            title=self.title,
            icon=chosen.value if chosen else "",
            status=status,
            clock=clock,
            level=level,
            text=text,
            extract=extract,
            timeout_ms=timeout_ms,
            position=self.position,
        )

    # ── what callers use ──────────────────────────────────────────

    def send(
        self,
        message: str,
        timeout: int | None = None,
        *,
        icon: OsdIcon | None = None,
        channel: NotifyChannel | None = None,
        urgency: Urgency | None = None,
        actions: Sequence[tuple[str, str]] | None = None,
        extract: bool = False,
    ) -> str | None:
        """Say something, on this notification's channel or an override.

        Urgency and actions only mean anything to the desktop channel; a card
        has no notion of either. With actions the call blocks for the popup's
        lifetime and returns the clicked action's name, or None. `extract`
        marks the message as a clipped fragment of something longer, which the
        card sets apart from a status."""
        if is_headless():
            log.debug("headless: dropping %r", message)
            return None

        if (channel or self.channel) is NotifyChannel.DESKTOP:
            return self._desktop(message, timeout, urgency, actions)

        self._card(timeout or self.HOLD_MS, icon, text=message, extract=extract)

    def tail(self, message: str) -> str:
        """The end of a growing text, flattened onto one line.

        The end rather than the start, because the interesting part of a text
        still arriving is the part that just arrived. `textwrap.shorten` keeps
        the other end, so it cannot do this job — only its word boundary is
        worth copying, so the line never opens mid-word."""
        flat = " ".join(message.split())
        if len(flat) > self.CARD_CHARS:
            clipped = flat[-self.CARD_CHARS :]
            _, _, rest = clipped.partition(" ")
            flat = f"…{rest}" if rest else clipped

        return flat

    def echo(
        self, chunks: Iterator[str], *, icon: OsdIcon | None = None
    ) -> Iterator[str]:
        """Pass chunks through untouched, showing the tail of them as they go.

        Wraps the stream rather than the sink, so a caller that only wants to
        watch and a caller that writes somewhere both get the same view."""
        seen = ""
        for chunk in chunks:
            seen += chunk
            self.send(self.tail(seen), icon=icon, extract=True)
            yield chunk

    def start(self) -> None:
        """Anchor the clock here, so `elapsed` counts from this instant.

        Said rather than inferred: only the caller knows which moment is the
        one worth measuring, and a card put up while the work is still being
        set up would otherwise become the anchor by accident. The stop stamp
        goes first, so a ticker reading the pair mid-call cannot pair a fresh
        anchor with the last run's freeze and read a negative clock."""
        self._stopped = 0.0
        self._started = time.monotonic()

    def freeze(self) -> None:
        """Hold the clock where it stands, leaving the card up.

        For work whose measured part ends before the job does - a recording
        that is over while the transcription behind it still runs."""
        self._stopped = time.monotonic()

    def thaw(self) -> None:
        """Run a frozen clock on from where it was held.

        The anchor moves after the stop stamp clears, so a ticker reading the
        pair mid-call sees the clock jump ahead for one frame, never backwards."""
        if not self._stopped:
            return
        held = time.monotonic() - self._stopped
        self._stopped = 0.0
        self._started += held

    def elapsed(
        self,
        status: str = "",
        timeout: int | None = None,
        *,
        icon: OsdIcon | None = None,
        level: float | None = None,
        extract: str = "",
    ) -> None:
        """Redraw the card with the time since the clock was anchored.

        `status` names the phase the work is in; `extract` is a clipped
        fragment of what it is working on, drawn under the clock."""
        if is_headless():
            return

        # Zero until someone anchors it, and held once frozen: an unanchored
        # clock counting from the first card is what `start` exists to avoid.
        now = self._stopped or time.monotonic()
        self._card(
            timeout or self.TICK_HOLD_MS,
            icon,
            status=status,
            clock=self.stamp(now - self._started if self._started else 0),
            level=None if level is None else min(max(level, 0.0), 1.0),
            text=extract,
            extract=True,
        )

    def playback(
        self,
        heard: float,
        total: float,
        *,
        done: bool,
        status: str = "",
        text: str = "",
        icon: OsdIcon | None = None,
        timeout: int | None = None,
    ) -> None:
        """Redraw the card with how far through a length the work is.

        `done` says the total is final; until then it is what has arrived so
        far, and the clock says so with a trailing ellipsis."""
        if is_headless():
            return

        self._card(
            timeout or self.TICK_HOLD_MS,
            icon,
            status=status,
            clock=f"{self.stamp(heard)} / {self.stamp(total)}{'' if done else '…'}",
            level=min(heard / total, 1.0) if total else 0.0,
            text=text,
        )

    @staticmethod
    def stamp(seconds: float) -> str:
        seconds = int(seconds)

        return f"{seconds // 60:d}:{seconds % 60:02d}"

    def dismiss(self, message: str = "", *, icon: OsdIcon | None = None) -> None:
        """Let it go, after a beat if there is a parting word."""
        self._started = self._stopped = 0.0
        if is_headless():
            return

        if message and self.channel is NotifyChannel.DESKTOP:
            self._desktop(message, None)
            return

        if message:
            self._card(self.DISMISS_MS, icon, text=message)
            return

        self.hide()

    def hide(self) -> None:
        """Take the card down and leave the clock alone, for work that is
        paused rather than over."""
        if is_headless():
            return

        self._overlay.hide(OverlaySlot.CARD)


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

    # Detached players still sounding. Swept on the next call, so a caller that
    # outlives many chimes does not collect a zombie per tone.
    _playing: ClassVar[list[subprocess.Popen[bytes]]] = []

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
                    mix[start + i] += (
                        accent * amplitude * envelope * math.sin(omega * i)
                    )

        # Normalise to the peak rather than trusting the partials to sum to
        # something predictable, so the level is the same whatever they are set
        # to.
        scale = self.GAIN / max(max(abs(v) for v in mix), 1e-9) * 32767

        return struct.pack(f"<{total}h", *(int(v * scale) for v in mix))

    def play(self, wait: bool = False) -> None:
        """Play the tone on its own, for a caller with no stream to ride.

        Detached, so the announcement does not delay the thing it announces:
        the popup or the write behind a chime lands a third of a second sooner.
        `wait` is for the callers that need the tone finished before the next
        line runs - suppression about to duck it, or a player about to take the
        sink."""
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
        if wait:
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

            return

        self._playing[:] = [p for p in self._playing if p.poll() is None]
        try:
            player = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=sys.stderr,
                stderr=sys.stderr,
                start_new_session=True,
            )
            assert player.stdin is not None
            # The whole tone goes over in one write with nothing reading yet:
            # a fifth of a second of 16-bit mono sits well inside the 64KiB a
            # pipe buffers before it blocks.
            player.stdin.write(pcm)
            player.stdin.close()
        except (FileNotFoundError, BrokenPipeError) as e:
            log.warning("chime failed: %s", e)
            return

        self._playing.append(player)


def _parent(pid: int) -> int:
    """The ppid field of `/proc/<pid>/stat`, read past the comm parentheses so
    a process named `(a b) c` cannot shift the offsets."""
    try:
        with open(f"/proc/{pid}/stat") as stat:
            return int(stat.read().rpartition(")")[2].split()[1])
    except OSError, IndexError, ValueError:
        return 0


def _terminal() -> str | None:
    """The nearest pty at or above this process.

    A hook or a forked worker is handed pipes, not a terminal, so its own
    descriptors say nothing about where its output is being watched. The
    process that owns the window is up the tree, and its pty is the thing
    worth ringing."""
    pid = os.getpid()
    while pid > 1:
        for fd in (1, 2, 0):
            try:
                target = os.readlink(f"/proc/{pid}/fd/{fd}")
            except OSError:
                continue
            if target.startswith("/dev/pts/"):
                return target
        pid = _parent(pid)

    return None


def bell() -> None:
    """Ring the terminal's own bell.

    The one channel that reaches a terminal rather than the desktop, so it
    marks the right window when the popup is missed - tmux flags the window,
    kitty raises an urgency hint.

    Never stdout, which for a pipe-involved script belongs to the payload and
    would carry the character downstream instead of to the terminal that can
    act on it."""
    for path in ("/dev/tty", _terminal()):
        if not path:
            continue
        try:
            with open(path, "w") as tty:
                tty.write("\a")
                tty.flush()
            return
        except OSError:
            continue

    log.debug("no terminal to ring")
