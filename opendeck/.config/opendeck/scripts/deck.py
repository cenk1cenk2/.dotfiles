"""OpenDeck plugin runtime shared by the deck's own plugins.

One loop per plugin: OpenDeck's WebSocket events, a poll of whatever the keys
show, and press/hold timing. A plugin supplies `poll`, `look` and the press
handlers; keys only receive a state or title when what they show changes."""

from __future__ import annotations

import base64
import configparser
import html
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import click
import websocket
from dotlib.cli import create_logger
from xdg.IconTheme import getIconPath

logging.getLogger("websocket").setLevel(logging.CRITICAL)

GTK_SETTINGS = Path.home() / ".config/gtk-3.0/settings.ini"
IMAGES = Path.home() / ".config/opendeck/images"


def icon(name: str, size: int = 144) -> Path | None:
    """`name` resolved through the GTK icon theme and what it inherits.

    Resolved on every call, so a theme switched or reinstalled while the
    plugin runs is picked up; None when no theme carries the name."""
    settings = configparser.ConfigParser()
    settings.read(GTK_SETTINGS)
    theme = settings.get("Settings", "gtk-icon-theme-name", fallback="hicolor")
    found = getIconPath(name, size, theme, ["svg", "png"])

    return Path(found) if found else None


@dataclass(frozen=True)
class Override:
    """The apps a key's press and hold drive instead of their usual targets.

    Either side's writing turns yellow and gains a tag naming the app: the
    label along the top for the press, the hold hint along the bottom for the
    hold. The writing itself stays, so the key still says what it does."""

    press: str | None = None
    hold: str | None = None


NO_OVERRIDE = Override()


@dataclass
class Key:
    action: str
    settings: dict
    shown: tuple[int, str] | None = None
    # What the key was last drawn from: the plugin's image or the state files,
    # the hold hint laid over it, whether that hold was firing, and the
    # override marked on it.
    drawn: tuple | None = None
    pressed_at: float | None = None
    held: bool = False
    fired_at: float = 0.0

    @property
    def label(self) -> str:
        return self.settings.get("label", "")


class Stream:
    """A long-lived command whose every stdout line is handed to `on_line`.

    For state a tool can push (`playerctl --follow`, `swaync-client
    --subscribe`, `nvidia-smi -l`), which costs one process for the plugin's
    life instead of one per poll. `run` starts it, or restarts it once it has
    exited, no more often than every RETRY_SECONDS; `stop` ends it, for when
    no key on screen needs it."""

    RETRY_SECONDS = 5.0

    log = logging.getLogger("deck.stream")

    def __init__(self, cmd: list[str], on_line: Callable[[str], None]):
        self.cmd = cmd
        self.on_line = on_line
        self.proc: subprocess.Popen | None = None
        self.started_at = 0.0

    def run(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            return
        if time.monotonic() - self.started_at < self.RETRY_SECONDS:
            return
        self.started_at = time.monotonic()
        self.log.debug("stream: %s", " ".join(self.cmd))
        try:
            self.proc = subprocess.Popen(
                # The kernel ends the stream when the plugin exits, however it
                # exits: OpenDeck kills a plugin without letting it clean up.
                ["setpriv", "--pdeathsig", "TERM", "--", *self.cmd],
                env={k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                start_new_session=True,
            )
        except OSError as e:
            self.log.warning("stream %s failed to start: %s", self.cmd[0], e)
            self.proc = None
            return
        threading.Thread(target=self.read, args=(self.proc,), daemon=True).start()

    def read(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            self.on_line(line.rstrip("\n"))
        proc.wait()

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGTERM)
        self.proc = None
        self.started_at = 0.0


class Plugin:
    POLL_SECONDS = 0.3
    HOLD_SECONDS = 0.3
    # How often a held key that repeats fires its hold again.
    REPEAT_SECONDS = 0.4
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

    def repeats(self, key: Key) -> bool:
        return False

    def hold_status(self, key: Key) -> str | None:
        """What a key shows in place of its status while its hold is active."""
        return None

    def hold_hint(self, key: Key) -> str | None:
        """What holding the key does, shown along its bottom edge at all times."""
        return self.hold_status(key)

    def override(self, key: Key) -> Override:
        """What the key's press and hold drive instead of their usual targets
        right now. Every key that follows focus goes through this, so yellow
        writing always means "this now drives <app>"."""
        return NO_OVERRIDE

    def key_down(self, context: str, key: Key) -> None:
        key.pressed_at, key.held = time.monotonic(), False

    def key_up(self, context: str, key: Key) -> None:
        if key.pressed_at is not None and not key.held:
            self.press(context, key)
        key.pressed_at, key.held = None, False
        self.render()

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

    # Marks a hinted image, so one read back from OpenDeck's saved copy is
    # unwrapped to the original instead of gaining a second band.
    HINT_MARK = 'data-deck-hint="1"'

    YELLOW = "#e5c07b"
    PLATE = "#17191e"

    @classmethod
    def tag(cls, text: str, y: int) -> str:
        """A yellow app name on a small plate, its top edge at `y`."""
        width = 16 + 12 * len(text)

        return (
            f'<rect x="{72 - width / 2}" y="{y}" width="{width}" height="28"'
            f' rx="6" fill="{cls.PLATE}" fill-opacity="0.9"/>'
            f'<text x="72" y="{y + 21}" font-family="Liberation Sans" font-size="21"'
            f' font-weight="bold" fill="{cls.YELLOW}" text-anchor="middle">'
            f"{html.escape(text)}</text>"
        )

    @classmethod
    def hinted(
        cls,
        image: str,
        hint: str | None,
        lit: bool = False,
        label: str = "",
        override: Override = NO_OVERRIDE,
    ) -> str:
        """`image` with `hint` in a band along its bottom edge, and the
        override's writing in yellow with a tag naming its app.

        The original is embedded whole rather than edited, so any image works
        and `unwrap` can always recover it. `lit` inverts the band, for while
        the hold it names is the one firing. An overridden press draws the
        label here, since OpenDeck's own title has no colour to set."""
        if lit:
            band, ink = cls.YELLOW, cls.PLATE
        else:
            band, ink = cls.PLATE, cls.YELLOW if override.hold else "#e5e5e5"
        # Sized for the device rather than the canvas: a key is drawn at half
        # this size, where anything under ~20px stops reading.
        parts = [f'<image href="{image}" width="144" height="144"/>']
        if override.press:
            parts.append(
                f'<rect width="144" height="30" fill="{cls.PLATE}" fill-opacity="0.9"/>'
                '<text x="72" y="22" font-family="Liberation Sans" font-size="21"'
                f' font-weight="bold" fill="{cls.YELLOW}" text-anchor="middle">'
                f"{html.escape(label)}</text>" + cls.tag(override.press, 32)
            )
        if override.hold:
            parts.append(cls.tag(override.hold, 84))
        if hint:
            parts.append(
                f'<rect y="114" width="144" height="30" fill="{band}" fill-opacity="0.9"/>'
                '<text x="72" y="136" font-family="Liberation Sans" font-size="21"'
                f' font-weight="bold" fill="{ink}" text-anchor="middle">'
                f"{html.escape(hint)}</text>"
            )
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="144" height="144"'
            f' viewBox="0 0 144 144" {cls.HINT_MARK}>{"".join(parts)}</svg>'
        )

        return f"data:image/svg+xml;base64,{base64.b64encode(svg.encode()).decode()}"

    @classmethod
    def unwrap(cls, path: Path) -> str | None:
        """A state image file as a data URI, minus any hint band on it.

        OpenDeck saves every image a plugin sets over the key's own file, so
        after the first hint the file holds the hinted copy."""
        try:
            data = path.read_bytes()
        except OSError:
            return None
        if path.suffix == ".svg":
            text = data.decode(errors="replace")
            if cls.HINT_MARK in text and (
                found := re.search(r'<image href="([^"]+)"', text)
            ):
                return found[1]
            mime = "image/svg+xml"
        else:
            mime = f"image/{path.suffix.lstrip('.').replace('jpg', 'jpeg')}"

        return f"data:{mime};base64,{base64.b64encode(data).decode()}"

    @classmethod
    def state_images(cls, context: str) -> dict[int, str]:
        """Each state's own image, from the files OpenDeck keeps per key.

        A context is `device.profile.controller.position.index`, and the files
        sit at `images/<device>/<profile>/<controller.position.index>/<state>`.
        A state drawn from a plugin's own icon has no file and is left out."""
        device, rest = context.split(".", 1)
        profile, controller, position, index = rest.rsplit(".", 3)
        folder = IMAGES / device / profile / f"{controller}.{position}.{index}"
        images = {}
        for path in folder.iterdir() if folder.is_dir() else ():
            if path.stem.isdigit() and (image := cls.unwrap(path)) is not None:
                images[int(path.stem)] = image

        return images

    def draw(self, context: str, key: Key) -> None:
        """Send the key's image when it, its hint or its hold changed.

        A plugin image goes to every state, as OpenDeck applies a stateless
        `setImage`; without one, the hint and tag are laid over each state's
        own file, and a key that drops them gets its plain files back."""
        override = self.override(key)
        hint = self.hold_hint(key) if self.holds(key) else None
        image = self.image(context, key)
        drawn = (image, hint, key.held, override)
        if drawn == key.drawn:
            return
        if (
            image is None
            and hint is None
            and key.drawn is None
            and override == NO_OVERRIDE
        ):
            return

        def dress(base: str) -> str:
            if hint or override != NO_OVERRIDE:
                return self.hinted(base, hint, key.held, key.label, override)

            return base

        if image is not None:
            self.send("setImage", context, {"image": dress(image)})
        else:
            for state, base in self.state_images(context).items():
                self.send("setImage", context, {"image": dress(base), "state": state})
        key.drawn = drawn

    def render(self) -> None:
        for context, key in self.keys.items():
            self.draw(context, key)

            look, status = self.look(context, key)
            if key.held and (held := self.hold_status(key)) is not None:
                status = held
            # An overridden press draws its label into the image, in yellow.
            label = "" if self.override(key).press else key.label
            shown = (look, "\n".join(part for part in (label, status) if part))
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
                self.keys[context].drawn = None
                self.render()
            case "keyDown" if context in self.keys:
                self.key_down(context, self.keys[context])
            case "keyUp" if context in self.keys:
                self.key_up(context, self.keys[context])

    def check_holds(self) -> None:
        now = time.monotonic()
        for context, key in self.keys.items():
            if key.pressed_at is None or not self.holds(key):
                continue
            if not key.held and now - key.pressed_at >= self.HOLD_SECONDS:
                key.held, key.fired_at = True, now
                self.hold(context, key)
                self.render()
            elif (
                key.held
                and self.repeats(key)
                and now - key.fired_at >= self.REPEAT_SECONDS
            ):
                key.fired_at = now
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
