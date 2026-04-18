#!/usr/bin/env python3
"""Subscribe to the Hyprland event socket and dispatch to handlers."""

import logging
import os
import socket
import sys
from abc import ABC, abstractmethod

from lib import Hyprctl

log = logging.getLogger("hyprland-events")

class EventHandler(ABC):
    def __init__(self, hypr: Hyprctl):
        self._hypr = hypr

    @abstractmethod
    def events(self) -> list[str]: ...

    @abstractmethod
    def handle(self, event: str, data: str) -> None: ...

    def on_start(self) -> None:
        pass

class TabletFollowFocus(EventHandler):
    def events(self) -> list[str]:
        return ["focusedmon"]

    def on_start(self) -> None:
        monitor = self._hypr.focused_monitor()
        if monitor:
            self._map(monitor["name"])

    def handle(self, event: str, data: str) -> None:
        self._map(data.split(",")[0])

    def _map(self, monitor: str) -> None:
        log.debug("mapping tablet to %s", monitor)
        self._hypr.keyword("input:tablet:output", monitor)

class HyprlandListener:
    def __init__(self, args, hypr: Hyprctl, handlers: list[EventHandler]):
        self.args = args
        self._hypr = hypr
        self._handlers = handlers
        self._dispatch: dict[str, list[EventHandler]] = {}
        for handler in handlers:
            for event in handler.events():
                self._dispatch.setdefault(event, []).append(handler)

    def run(self) -> None:
        log.info(
            "subscribing to events: %s",
            ", ".join(sorted(self._dispatch.keys())),
        )
        for handler in self._handlers:
            handler.on_start()

        path = self._socket_path()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(path)
        log.info("connected to %s", path)

        self._read_loop(sock)

    def _read_loop(self, sock: socket.socket) -> None:
        buf = b""
        while True:
            data = sock.recv(4096)
            if not data:
                log.warning("socket closed, exiting")
                break

            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                decoded = line.decode("utf-8", errors="replace")
                if ">>" not in decoded:
                    continue

                event, _, payload = decoded.partition(">>")
                for handler in self._dispatch.get(event, []):
                    try:
                        handler.handle(event, payload)
                    except Exception:
                        log.exception(
                            "handler %s failed", handler.__class__.__name__,
                        )

    @staticmethod
    def _socket_path() -> str:
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
        instance_sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
        if not runtime_dir or not instance_sig:
            log.error("Hyprland environment variables not set")
            sys.exit(1)

        return f"{runtime_dir}/hypr/{instance_sig}/.socket2.sock"

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    hypr = Hyprctl()
    handlers: list[EventHandler] = [
        TabletFollowFocus(hypr),
    ]
    HyprlandListener(args=None, hypr=hypr, handlers=handlers).run()

if __name__ == "__main__":
    main()
