"""Hyprland IPC for the deck plugins: requests, the event socket, and the
focused window kept current from it."""

from __future__ import annotations

import glob
import json
import logging
import os
import socket
import threading
import time
from typing import Any


class Hyprland:
    """The subset of Hyprland IPC the deck needs.

    Hyprctl is the hyprland project's; this is the deck's own copy, the same
    way `jumpy` carries one, so the two projects stay independent."""

    log = logging.getLogger("hyprland.ipc")

    def __init__(self) -> None:
        runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        signature = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
        found = (
            [os.path.join(runtime, "hypr", signature)]
            if signature
            else sorted(
                glob.glob(os.path.join(runtime, "hypr", "*")), key=os.path.getmtime
            )
        )
        self.dir = found[-1] if found else None

    def request(self, message: str) -> str | None:
        if self.dir is None:
            return None
        self.log.debug("ipc: %s", message)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.connect(os.path.join(self.dir, ".socket.sock"))
                sock.sendall(message.encode())
                chunks = []
                while chunk := sock.recv(8192):
                    chunks.append(chunk)
        except OSError as e:
            self.log.debug("ipc failed: %s", e)
            return None

        return b"".join(chunks).decode()

    def query(self, what: str) -> Any:
        response = self.request(f"j/{what}")
        try:
            return json.loads(response) if response else None
        except ValueError:
            return None

    def dispatch(self, expr: str) -> None:
        """Run a Lua dispatcher; 0.55+ no longer takes the legacy verb form."""
        response = self.request(f"dispatch {expr}")
        if response != "ok":
            self.log.warning("dispatch %s: %s", expr, response)

    def events(self, on_event) -> None:
        """Call `on_event` for every line on the event socket, until it closes."""
        if self.dir is None:
            return
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(os.path.join(self.dir, ".socket2.sock"))
            buffer = b""
            while chunk := sock.recv(4096):
                buffer += chunk
                *lines, buffer = buffer.split(b"\n")
                for line in lines:
                    on_event(line.decode(errors="replace"))


class Focus:
    """The focused window, kept current from Hyprland's event socket, so a key
    press reads it without a request of its own."""

    RETRY_SECONDS = 5.0
    # Events after which a window list read earlier may be wrong.
    WINDOW_EVENTS = ("openwindow", "closewindow", "movewindow", "windowtitle")

    log = logging.getLogger("hyprland.focus")

    def __init__(self, hypr: Hyprland) -> None:
        self.hypr = hypr
        self.window_class = ""
        self.title = ""
        self.address = ""
        # Bumped on every window change, and on every reconnect since events
        # missed while down never arrive; a reader re-lists when it moved.
        self.windows = 0
        threading.Thread(target=self.follow, daemon=True).start()

    def follow(self) -> None:
        while True:
            # Seeded on every connect: what changed while the socket was down
            # never arrives as an event.
            active = self.hypr.query("activewindow") or {}
            self.window_class = active.get("class", "")
            self.title = active.get("title", "")
            self.address = active.get("address", "")
            self.windows += 1
            try:
                self.hypr.events(self.on_event)
            except OSError as e:
                self.log.warning("event socket closed: %s", e)
            time.sleep(self.RETRY_SECONDS)

    def on_event(self, line: str) -> None:
        event, _, data = line.partition(">>")
        match event:
            # A title may hold commas; a class does not.
            case "activewindow":
                self.window_class, _, self.title = data.partition(",")
            case "activewindowv2":
                self.address = f"0x{data}" if data else ""
            case _ if event in self.WINDOW_EVENTS:
                self.windows += 1
