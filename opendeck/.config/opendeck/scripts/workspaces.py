#!/usr/bin/env -S sh -c 'd="$(dirname "$0")"; uv sync -q --project "$d" && exec "$d/.venv/bin/python" "$0" "$@"'

from __future__ import annotations

import base64
import glob
import itertools
import json
import logging
import math
import os
import re
import socket
import threading
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar

from deck import Key, Plugin, command


class Action(StrEnum):
    SLOT = "dev.kilic.workspaces.slot"
    PAGE = "dev.kilic.workspaces.page"
    NEW = "dev.kilic.workspaces.new"


@dataclass
class Workspace:
    id: int
    windows: int
    monitor: str
    # Window classes, most recently focused first.
    apps: list[str]


class Hyprland:
    """The subset of Hyprland IPC the deck needs.

    Hyprctl is the hyprland project's; this is the deck's own copy, the same
    way `jumpy` carries one, so the two projects stay independent."""

    log = logging.getLogger("workspaces.ipc")

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
        """Call `on_event` for every line on the event socket, forever."""
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


class Icons:
    """App icons by window class, as the hyprland project's window_icons maps them."""

    APPLICATIONS = (
        Path.home() / ".local/share/applications",
        Path("/usr/share/applications"),
        Path("/var/lib/flatpak/exports/share/applications"),
        Path.home() / ".local/share/flatpak/exports/share/applications",
    )
    THEMES = (
        "/usr/share/icons/Tela-yellow-dark/scalable/apps",
        "/usr/share/icons/hicolor/scalable/apps",
        "/usr/share/icons/hicolor/256x256/apps",
        "/usr/share/icons/hicolor/128x128/apps",
        "/usr/share/icons/hicolor/48x48/apps",
        "/usr/share/pixmaps",
    )

    def __init__(self) -> None:
        self.names: dict[str, str] = {}
        self.uris: dict[str, str | None] = {}
        for directory in self.APPLICATIONS:
            for path in directory.glob("*.desktop") if directory.exists() else ():
                try:
                    text = path.read_text(errors="replace")
                except OSError:
                    continue
                icon = re.search(r"^Icon=(.+)$", text, re.MULTILINE)
                if not icon:
                    continue
                keys = [path.stem]
                if wm_class := re.search(r"^StartupWMClass=(.+)$", text, re.MULTILINE):
                    keys.append(wm_class[1].strip())
                for key in keys:
                    self.names.setdefault(key.lower(), icon[1].strip())

    def name(self, window_class: str) -> str:
        lowered = window_class.lower()
        for candidate in (
            lowered,
            lowered.split(".")[-1],
            lowered.removesuffix("-browser"),
        ):
            if candidate in self.names:
                return self.names[candidate]

        return lowered

    def uri(self, window_class: str) -> str | None:
        """The class's icon as a data URI, or None when no theme has it."""
        if window_class not in self.uris:
            self.uris[window_class] = self.lookup(self.name(window_class))

        return self.uris[window_class]

    @staticmethod
    def lookup(name: str) -> str | None:
        for directory in Icons.THEMES:
            for ext, mime in (("svg", "image/svg+xml"), ("png", "image/png")):
                path = Path(directory) / f"{name}.{ext}"
                if path.exists():
                    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"

        return None


class WorkspacesPlugin(Plugin):
    """Stream Deck plugin for Hyprland workspaces, paged ten to a page.

    Only workspaces that exist are listed, sorted by number, so gaps take no
    key. Hyprland's event socket marks the view stale; the next poll redraws."""

    PER_PAGE = 10
    FOCUSED = "#c678dd"
    VISIBLE = "#61afef"
    DARK = "#17191e"
    ARROW: ClassVar[dict[str, str]] = {
        "prev": "m11.705 12.59-4.58-4.59 4.58-4.59-1.41-1.41-6 6 6 6z",
        "next": "M 4.59,12.59 9.17,8 4.59,3.41 6,2 12,8 6,14 Z",
    }
    PLUS = "M 7,1 V 7 H 1 v 2 h 6 v 6 H 9 V 9 h 6 V 7 H 9 V 1 Z"
    EVENTS = (
        "workspace",
        "focusedmon",
        "createworkspace",
        "destroyworkspace",
        "moveworkspace",
        "openwindow",
        "closewindow",
        "movewindow",
        "activewindow",
    )

    log = logging.getLogger("workspaces-deck")

    def __init__(self, ws):
        super().__init__(ws)
        self.hypr = Hyprland()
        self.icons = Icons()
        self.workspaces: list[Workspace] = []
        self.focused: int | None = None
        self.visible: set[int] = set()
        self.page = 0
        self.stale = threading.Event()
        self.stale.set()
        threading.Thread(target=self.listen, daemon=True).start()

    def listen(self) -> None:
        def on_event(line: str) -> None:
            if line.split(">>", 1)[0] in self.EVENTS:
                self.stale.set()

        try:
            self.hypr.events(on_event)
        except OSError as e:
            self.log.warning("event socket closed: %s", e)

    def poll(self) -> None:
        if not self.stale.is_set():
            return
        self.stale.clear()

        monitors = self.hypr.query("monitors") or []
        clients = self.hypr.query("clients") or []
        spaces = self.hypr.query("workspaces") or []
        places = self.places(monitors)
        apps: dict[int, list[str]] = {}
        for client in sorted(clients, key=lambda c: c["focusHistoryID"]):
            apps.setdefault(client["workspace"]["id"], []).append(client["class"])

        self.workspaces = [
            Workspace(
                w["id"],
                w["windows"],
                places.get(w["monitor"], ""),
                apps.get(w["id"], []),
            )
            for w in sorted(spaces, key=lambda w: w["id"])
            if w["id"] > 0
        ]
        self.visible = {m["activeWorkspace"]["id"] for m in monitors}
        focused = next(
            (m["activeWorkspace"]["id"] for m in monitors if m["focused"]), None
        )
        if focused != self.focused:
            self.focused = focused
            self.page = self.page_of(focused)
        self.page = min(self.page, self.pages() - 1)

    @staticmethod
    def places(monitors: list[dict]) -> dict[str, str]:
        """Each monitor named by where it sits, which reads better than DP-1.

        A stack reads top to bottom and a row left to right; a layout that is
        neither keeps the connector names."""
        if len(monitors) < 2:
            return {m["name"]: "" for m in monitors}

        def spread(axis: str, size: str) -> bool:
            edges = sorted((m[axis], m[axis] + m[size]) for m in monitors)
            return all(a[1] <= b[0] for a, b in itertools.pairwise(edges))

        for axis, size, names in (
            ("y", "height", ("top", "middle", "bottom")),
            ("x", "width", ("left", "center", "right")),
        ):
            if spread(axis, size):
                ordered = sorted(monitors, key=lambda m: m[axis])
                labels = (
                    names[::2]
                    if len(ordered) == 2
                    else names
                    if len(ordered) == 3
                    else [f"{names[0]} {i + 1}" for i in range(len(ordered))]
                )
                return {
                    m["name"]: label for m, label in zip(ordered, labels, strict=True)
                }

        return {m["name"]: m["name"] for m in monitors}

    def pages(self) -> int:
        return max(1, math.ceil(len(self.workspaces) / self.PER_PAGE))

    def page_of(self, wid: int | None) -> int:
        ids = [w.id for w in self.workspaces]
        return ids.index(wid) // self.PER_PAGE if wid in ids else self.page

    def slot(self, key: Key) -> Workspace | None:
        index = self.page * self.PER_PAGE + int(key.settings["index"])
        return self.workspaces[index] if index < len(self.workspaces) else None

    def unused(self) -> int:
        ids = {w.id for w in self.workspaces}
        return next(n for n in range(1, len(ids) + 2) if n not in ids)

    @staticmethod
    def uri(svg: str) -> str:
        return f"data:image/svg+xml;base64,{base64.b64encode(svg.encode()).decode()}"

    def tile(self, colour: str | None, inner: str) -> str:
        """A key in the deck's style: dark plate on top, a tile below it."""
        tile = (
            f'<rect y="36" width="144" height="108" fill="{colour}"/>' if colour else ""
        )

        return self.uri(
            '<svg xmlns="http://www.w3.org/2000/svg" width="144" height="144" viewBox="0 0 144 144">'
            f'<rect width="144" height="144" fill="{self.DARK}"/>{tile}{inner}</svg>'
        )

    def app_icons(self, space: Workspace) -> str:
        """One large icon, or up to four in a grid with a count of the rest."""
        icons = [
            uri for app in dict.fromkeys(space.apps) if (uri := self.icons.uri(app))
        ]
        if len(icons) == 1:
            return f'<image href="{icons[0]}" x="36" y="48" width="72" height="72"/>'

        cells = ((24, 44), (76, 44), (24, 94), (76, 94))
        extra = len(icons) - 4
        shown = icons[:3] if extra > 0 else icons[:4]
        out = "".join(
            f'<image href="{uri}" x="{x}" y="{y}" width="44" height="44"/>'
            for uri, (x, y) in zip(shown, cells, strict=False)
        )
        if extra > 0:
            out += (
                '<text x="98" y="126" font-family="Liberation Sans" font-size="26"'
                f' font-weight="bold" fill="#e5e5e5" text-anchor="middle">+{extra + 1}</text>'
            )

        return out

    def glyph(self, path: str, colour: str) -> str:
        return (
            f'<g transform="translate(30.000 46.000) scale(5.2500)" fill="{colour}">'
            f'<path d="{path}"/></g>'
        )

    def image(self, context: str, key: Key) -> str | None:
        match key.action:
            case Action.SLOT:
                space = self.slot(key)
                if space is None:
                    return self.tile(None, "")
                colour = (
                    self.FOCUSED
                    if space.id == self.focused
                    else self.VISIBLE
                    if space.id in self.visible
                    else None
                )
                return self.tile(colour, self.app_icons(space))
            case Action.PAGE if key.settings["direction"] == "current":
                return self.tile(None, "")
            case Action.PAGE:
                direction = key.settings["direction"]
                more = (
                    self.page > 0
                    if direction == "prev"
                    else self.page < self.pages() - 1
                )
                colour = self.FOCUSED if more else "#3e4451"
                return self.tile(colour, self.glyph(self.ARROW[direction], self.DARK))
            case Action.NEW:
                return self.tile(self.FOCUSED, self.glyph(self.PLUS, self.DARK))

        raise ValueError(f"unknown action {key.action}")

    def look(self, context: str, key: Key) -> tuple[int, str]:
        match key.action:
            case Action.SLOT:
                space = self.slot(key)
                if space is None:
                    return 0, ""
                return 0, f"{space.id} {space.monitor}".strip()
            case Action.PAGE:
                match key.settings["direction"]:
                    case "current":
                        return 0, f"{self.page + 1}/{self.pages()}"
                    case "prev" if self.page > 0:
                        return (
                            0,
                            f"{(self.page - 1) * self.PER_PAGE + 1}–{self.page * self.PER_PAGE}",
                        )
                    case "next" if self.page < self.pages() - 1:
                        start = (self.page + 1) * self.PER_PAGE + 1
                        return 0, f"{start}–{start + self.PER_PAGE - 1}"
                return 0, ""
            case Action.NEW:
                return 0, str(self.unused())

        raise ValueError(f"unknown action {key.action}")

    def flip(self, page: int) -> None:
        self.page = max(0, min(page, self.pages() - 1))
        self.render()

    def press(self, context: str, key: Key) -> None:
        match key.action:
            case Action.SLOT if (space := self.slot(key)) is not None:
                self.hypr.dispatch(f"hl.dsp.focus({{ workspace = {space.id} }})")
            case Action.PAGE:
                match key.settings["direction"]:
                    case "prev":
                        self.flip(self.page - 1)
                    case "next":
                        self.flip(self.page + 1)
                    case "current":
                        self.flip(self.page_of(self.focused))
            case Action.NEW:
                self.hypr.dispatch(f"hl.dsp.focus({{ workspace = {self.unused()} }})")

    def holds(self, key: Key) -> bool:
        return key.action in (Action.SLOT, Action.NEW) or (
            key.action == Action.PAGE and key.settings["direction"] != "current"
        )

    def hold_status(self, key: Key) -> str | None:
        match key.action:
            case Action.PAGE:
                return "first" if key.settings["direction"] == "prev" else "last"

        return "move here"

    def hold(self, context: str, key: Key) -> None:
        match key.action:
            case Action.SLOT if (space := self.slot(key)) is not None:
                self.hypr.dispatch(
                    f'hl.dsp.window.move({{ workspace = "{space.id}" }})'
                )
            case Action.NEW:
                self.hypr.dispatch(
                    f'hl.dsp.window.move({{ workspace = "{self.unused()}" }})'
                )
            case Action.PAGE:
                self.flip(
                    0 if key.settings["direction"] == "prev" else self.pages() - 1
                )


if __name__ == "__main__":
    command(WorkspacesPlugin)()
