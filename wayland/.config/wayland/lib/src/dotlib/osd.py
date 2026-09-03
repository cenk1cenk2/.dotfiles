"""An on-screen card for work the user started and is waiting on.

Driven through `swayosd`, which is already running for volume and brightness,
so this is a view model and a bus call rather than a window of our own. What
that buys and what it costs: no GTK anywhere in these scripts, no extra unit,
one row on screen — and in exchange no waveform, since swayosd's segmented
progress is a single value across N segments rather than N heights.

The bus rather than `swayosd-client`: the CLI is a thin wrapper over one
`HandleAction` call, and a card that ticks a timer would otherwise mean a
process per second. Captured from `dbus-monitor` while the client ran, since
the interface is not documented:

    HandleAction("CUSTOM-MESSAGE", "<text>", [("CUSTOM-ICON", "<name>"),
                                              ("DURATION", "<ms>")])

Every action replaces the row in place and restarts the hide timer, which is
what makes re-firing the right way to hold a card open."""

from __future__ import annotations

import logging
import subprocess
import time
from enum import StrEnum

from .desktop import is_headless

log = logging.getLogger(__name__)


class OsdIcon(StrEnum):
    """Freedesktop icon names, so the card says what kind of work this is."""

    MIC = "audio-input-microphone"
    SPEAKER = "audio-speakers"
    THINKING = "system-run"
    DONE = "object-select"
    ERROR = "dialog-error"


class Osd:
    """One card on the shared surface, refreshed until the work is done.

    Not an adapter over a Protocol, because there is exactly one backend: a
    second one earns the indirection, this does not."""

    BUS = "org.erikreider.swayosd-server"
    PATH = "/org/erikreider/swayosd"
    INTERFACE = "org.erikreider.swayosd"

    # swayosd hides a card when its timer expires and offers no explicit hide,
    # so the card is held open by re-firing inside this window and dismissed by
    # firing once with a short one.
    HOLD_MS = 2000
    DISMISS_MS = 200
    # A wedged bus call must never delay the recording it is describing.
    TIMEOUT = 2.0

    def __init__(self, title: str = "", icon: OsdIcon | None = None):
        self.title = title
        self.icon = icon
        self._started = 0.0

    def _call(self, action: str, value: str, options: list[tuple[str, str]]) -> None:
        flat: list[str] = []
        for key, option in options:
            flat += [key, option]
        cmd = [
            "busctl",
            "--user",
            "call",
            self.BUS,
            self.PATH,
            self.INTERFACE,
            "HandleAction",
            "ssa(ss)",
            action,
            value,
            str(len(options)),
            *flat,
        ]
        log.debug("spawn: %s", " ".join(cmd))
        try:
            subprocess.run(
                cmd, capture_output=True, timeout=self.TIMEOUT, check=False
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            # The card is never the point of the work it describes, so a
            # missing bus is worth a line in the log and nothing more.
            log.warning("osd call failed: %s", e)

    def show(self, text: str, *, icon: OsdIcon | None = None) -> None:
        """Put the card up, or replace what it says."""
        if is_headless():
            return

        if not self._started:
            self._started = time.monotonic()
        options = [("DURATION", str(self.HOLD_MS))]
        chosen = icon or self.icon
        if chosen:
            options.insert(0, ("CUSTOM-ICON", chosen.value))
        message = f"{self.title}  {text}" if self.title else text
        self._call("CUSTOM-MESSAGE", message, options)

    def elapsed(self, text: str = "", *, icon: OsdIcon | None = None) -> None:
        """Refresh the card with the time since it first went up.

        The clock is ours rather than swayosd's: it renders whatever string it
        is handed and has no notion of a running timer."""
        seconds = int(time.monotonic() - self._started) if self._started else 0
        stamp = f"{seconds // 60:d}:{seconds % 60:02d}"
        self.show(f"{stamp}  {text}".rstrip(), icon=icon)

    def dismiss(self, text: str = "", *, icon: OsdIcon | None = None) -> None:
        """Let the card go, after a beat if there is a parting message."""
        if is_headless():
            return

        self._started = 0.0
        if not text:
            return

        options = [("DURATION", str(self.DISMISS_MS))]
        chosen = icon or self.icon
        if chosen:
            options.insert(0, ("CUSTOM-ICON", chosen.value))
        message = f"{self.title}  {text}" if self.title else text
        self._call("CUSTOM-MESSAGE", message, options)
