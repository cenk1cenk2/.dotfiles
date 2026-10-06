"""OpenDeck plugin runtime shared by the deck's own plugins.

One loop per plugin: OpenDeck's WebSocket events, a poll of whatever the keys
show, and press/hold timing. A plugin supplies `poll`, `look` and the press
handlers; keys only receive a state or title when what they show changes."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass

import click
import websocket
from dotlib.cli import create_logger

logging.getLogger("websocket").setLevel(logging.CRITICAL)


@dataclass
class Key:
    action: str
    settings: dict
    shown: tuple[int, str] | None = None
    drawn: str | None = None
    pressed_at: float | None = None
    held: bool = False

    @property
    def label(self) -> str:
        return self.settings.get("label", "")


class Plugin:
    POLL_SECONDS = 0.3
    HOLD_SECONDS = 0.3
    # Every key is re-sent this often, so an update OpenDeck dropped heals.
    REFRESH_SECONDS = 5.0
    KEY: type[Key] = Key

    log = logging.getLogger("deck")

    def __init__(self, ws: websocket.WebSocket):
        self.ws = ws
        self.keys: dict[str, Key] = {}
        self.children: list[subprocess.Popen] = []

    def poll(self) -> None:
        raise NotImplementedError

    def look(self, context: str, key: Key) -> tuple[int, str]:
        """The state index and status line a key should show."""
        raise NotImplementedError

    def image(self, context: str, key: Key) -> str | None:
        """A data URI the key should draw instead of its state images."""
        return None

    def press(self, context: str, key: Key) -> None:
        raise NotImplementedError

    def holds(self, key: Key) -> bool:
        return False

    def hold(self, context: str, key: Key) -> None:
        raise NotImplementedError

    def key_down(self, context: str, key: Key) -> None:
        key.pressed_at, key.held = time.monotonic(), False

    def key_up(self, context: str, key: Key) -> None:
        if key.pressed_at is not None and not key.held:
            self.press(context, key)
        key.pressed_at = None

    def send(self, event: str, context: str, payload: dict) -> None:
        self.ws.send(
            json.dumps({"event": event, "context": context, "payload": payload})
        )

    def spawn(self, cmd: list[str]) -> None:
        self.log.info("spawn: %s", " ".join(cmd))
        self.children.append(
            subprocess.Popen(
                cmd,
                # The plugin's own venv would leak into every uv script it runs.
                env={k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"},
                stdin=subprocess.DEVNULL,
                stdout=sys.stderr,
                stderr=sys.stderr,
                start_new_session=True,
            )
        )

    def render(self) -> None:
        for context, key in self.keys.items():
            if (image := self.image(context, key)) is not None and image != key.drawn:
                self.send("setImage", context, {"image": image})
                key.drawn = image

            look, status = self.look(context, key)
            shown = (look, "\n".join(part for part in (key.label, status) if part))
            if shown == key.shown:
                continue

            self.send("setState", context, {"state": shown[0]})
            self.send("setTitle", context, {"title": shown[1]})
            key.shown = shown

    def handle(self, message: dict) -> None:
        event, context = message.get("event"), message.get("context")
        payload = message.get("payload") or {}
        self.log.debug("event: %s %s", event, context)

        match event:
            case "willAppear":
                self.keys[context] = self.KEY(
                    message["action"], payload.get("settings") or {}
                )
                self.poll()
                self.render()
            case "willDisappear":
                self.keys.pop(context, None)
            case "didReceiveSettings" if context in self.keys:
                self.keys[context].settings = payload.get("settings") or {}
                self.keys[context].shown = None
                self.render()
            case "keyDown" if context in self.keys:
                self.key_down(context, self.keys[context])
            case "keyUp" if context in self.keys:
                self.key_up(context, self.keys[context])

    def check_holds(self) -> None:
        now = time.monotonic()
        for context, key in self.keys.items():
            if key.pressed_at is None or key.held or not self.holds(key):
                continue
            if now - key.pressed_at >= self.HOLD_SECONDS:
                key.held = True
                self.hold(context, key)

    def serve(self) -> None:
        self.ws.settimeout(0.05)
        next_poll = next_refresh = 0.0
        while True:
            try:
                self.handle(json.loads(self.ws.recv()))
            except websocket.WebSocketTimeoutException:
                pass

            self.check_holds()
            if time.monotonic() >= next_refresh:
                for key in self.keys.values():
                    key.shown = None
                next_refresh = time.monotonic() + self.REFRESH_SECONDS
            if time.monotonic() >= next_poll:
                self.children = [c for c in self.children if c.poll() is None]
                if self.keys:
                    self.poll()
                    self.render()
                next_poll = time.monotonic() + self.POLL_SECONDS


def command(plugin: type[Plugin]) -> click.Command:
    """The entry point OpenDeck launches, with its single-dash arguments."""

    @click.command()
    @click.option(
        "-port", "port", type=int, required=True, help="OpenDeck WebSocket port."
    )
    @click.option(
        "-pluginUUID", "plugin_uuid", required=True, help="Registration UUID."
    )
    @click.option(
        "-registerEvent", "register_event", required=True, help="Registration event."
    )
    @click.option("-info", "info", default="{}", help="Host info JSON.")
    @click.option("-v", "--verbose", is_flag=True, help="Debug logging.")
    def cli(
        port: int, plugin_uuid: str, register_event: str, info: str, verbose: bool
    ) -> None:
        create_logger(verbose)
        ws = websocket.create_connection(f"ws://localhost:{port}")
        ws.send(json.dumps({"event": register_event, "uuid": plugin_uuid}))
        plugin.log.info("registered %s on port %d", plugin_uuid, port)
        try:
            plugin(ws).serve()
        except websocket.WebSocketConnectionClosedException:
            plugin.log.info("OpenDeck closed the connection")

    return cli
