#!/usr/bin/env python3

import json
import logging
import os
import socket
import subprocess
import sys
from abc import ABC, abstractmethod

log = logging.getLogger("hyprland-events")

def hyprctl(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["hyprctl", *args],
        capture_output=True,
        text=True,
    )

def hyprctl_json(args: list[str]):
    result = hyprctl(["-j", *args])
    if result.returncode != 0:
        return None

    return json.loads(result.stdout)

def get_focused_monitor() -> str | None:
    monitors = hyprctl_json(["monitors"])
    if not monitors:
        return None

    for monitor in monitors:
        if monitor.get("focused"):
            return monitor["name"]

    return None

class EventHandler(ABC):
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
        monitor = get_focused_monitor()
        if monitor:
            self._map(monitor)

    def handle(self, event: str, data: str) -> None:
        monitor_name = data.split(",")[0]
        self._map(monitor_name)

    def _map(self, monitor: str) -> None:
        log.debug("mapping tablet to %s", monitor)
        hyprctl(["keyword", "input:tablet:output", monitor])

HANDLERS: list[EventHandler] = [
    TabletFollowFocus(),
]

def get_socket_path() -> str:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    instance_sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    if not runtime_dir or not instance_sig:
        log.error("Hyprland environment variables not set")
        sys.exit(1)

    return f"{runtime_dir}/hypr/{instance_sig}/.socket2.sock"

def build_dispatch() -> dict[str, list[EventHandler]]:
    dispatch: dict[str, list[EventHandler]] = {}
    for handler in HANDLERS:
        for event in handler.events():
            dispatch.setdefault(event, []).append(handler)

    return dispatch

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    dispatch = build_dispatch()
    subscribed = set(dispatch.keys())
    log.info("subscribing to events: %s", ", ".join(sorted(subscribed)))

    for handler in HANDLERS:
        handler.on_start()

    sock_path = get_socket_path()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(sock_path)
    log.info("connected to %s", sock_path)

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
            if event not in subscribed:
                continue

            for handler in dispatch[event]:
                try:
                    handler.handle(event, payload)
                except Exception:
                    log.exception("handler %s failed", handler.__class__.__name__)

if __name__ == "__main__":
    main()
