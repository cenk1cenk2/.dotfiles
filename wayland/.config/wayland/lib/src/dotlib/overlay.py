"""Client for the overlay daemon (`scripts/overlay.py serve`).

One JSON object per line over a unix socket in the runtime dir. The
connection is opened on first use and kept, so a card that ticks five times a
second costs a write rather than a process. A daemon that is not running
drops the message: an overlay is never the point of the work it describes."""

from __future__ import annotations

import json
import logging
import os
import socket
from enum import StrEnum
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class OverlaySlot(StrEnum):
    """The surfaces the daemon owns. Each holds one thing at a time."""

    CARD = "card"
    HINTS = "hints"


class OverlayPosition(StrEnum):
    """Where a slot is drawn. Progress belongs in a corner, out of the way of
    the work; something that wants a decision now goes in the middle."""

    CENTER = "center"
    BOTTOM_RIGHT = "bottom-right"


class OverlayOp(StrEnum):
    SHOW = "show"
    HIDE = "hide"


class Overlay:
    SOCKET = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "overlay.sock"
    # A wedged daemon must never delay the caller.
    TIMEOUT = 0.5

    def __init__(self, path: Path = SOCKET):
        self.path = path
        self._sock: socket.socket | None = None

    def _connect(self) -> socket.socket:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.TIMEOUT)
        sock.connect(str(self.path))
        return sock

    def send(self, slot: OverlaySlot, op: OverlayOp, **payload: Any) -> None:
        line = (json.dumps({"slot": slot, "op": op, **payload}) + "\n").encode()
        # Twice: a kept connection goes stale when the daemon restarts, and
        # only the write after that tells us.
        for _ in range(2):
            try:
                if self._sock is None:
                    self._sock = self._connect()
                self._sock.sendall(line)
                return
            except OSError as e:
                log.debug("overlay send failed: %s", e)
                if self._sock is not None:
                    self._sock.close()
                    self._sock = None

    def show(self, slot: OverlaySlot, **payload: Any) -> None:
        self.send(slot, OverlayOp.SHOW, **payload)

    def hide(self, slot: OverlaySlot) -> None:
        self.send(slot, OverlayOp.HIDE)
