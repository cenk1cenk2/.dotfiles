#!/usr/bin/env -S sh -c 'd="$(dirname "$0")"; uv sync -q --project "$d" && exec "$d/.venv/bin/python" "$0" "$@"'

from __future__ import annotations

import json
import logging
import os
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, ClassVar

import click
from dotlib.cli import create_logger
from dotlib.overlay import Overlay, OverlayOp, OverlayPosition, OverlaySlot

# The layer-shell library has to be loaded ahead of libwayland-client, which
# importing Gtk pulls in, so the process starts over with it preloaded.
PRELOAD = "/usr/lib/libgtk4-layer-shell.so"
if PRELOAD not in os.environ.get("LD_PRELOAD", ""):
    os.environ["LD_PRELOAD"] = ":".join(
        filter(None, [PRELOAD, os.environ.get("LD_PRELOAD")])
    )
    os.execv(sys.executable, [sys.executable, *sys.argv])

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
gi.require_version("Pango", "1.0")

from gi.repository import Gio, GLib, Gtk, Pango
from gi.repository import Gtk4LayerShell as LayerShell


class Surface:
    """A borderless layer-shell window drawn entirely by the GTK theme: its
    background, label classes, progress bar and keycaps. Only spacing is ours."""

    MARGIN = 12
    EDGES: ClassVar[dict[OverlayPosition, tuple[LayerShell.Edge, ...]]] = {
        OverlayPosition.CENTER: (),
        OverlayPosition.BOTTOM_RIGHT: (LayerShell.Edge.BOTTOM, LayerShell.Edge.RIGHT),
    }

    def __init__(self, app: Gtk.Application, namespace: str, position: OverlayPosition):
        self.window = Gtk.ApplicationWindow(
            application=app, decorated=False, css_classes=["background", "csd"]
        )
        LayerShell.init_for_window(self.window)
        LayerShell.set_namespace(self.window, namespace)
        LayerShell.set_layer(self.window, LayerShell.Layer.OVERLAY)
        LayerShell.set_keyboard_mode(self.window, LayerShell.KeyboardMode.NONE)
        self.default = position
        self.position: OverlayPosition | None = None
        self.place(position)

    def place(self, position: OverlayPosition) -> None:
        # Every anchor or margin call reconfigures the surface, which a card
        # redrawn five times a second shows as flicker.
        if position is self.position:
            return
        self.position = position
        edges = self.EDGES[position]
        for edge in (
            LayerShell.Edge.TOP,
            LayerShell.Edge.BOTTOM,
            LayerShell.Edge.LEFT,
            LayerShell.Edge.RIGHT,
        ):
            LayerShell.set_anchor(self.window, edge, edge in edges)
            LayerShell.set_margin(
                self.window, edge, self.MARGIN if edge in edges else 0
            )

    @staticmethod
    def medium() -> Pango.AttrList:
        """Body text one weight up from the theme's regular, which reads thin
        over whatever is behind a floating surface."""
        attrs = Pango.AttrList()
        attrs.insert(Pango.attr_weight_new(Pango.Weight.MEDIUM))

        return attrs

    def frame(self, child: Gtk.Widget) -> None:
        for side in ("top", "bottom", "start", "end"):
            child.set_property(f"margin-{side}", 16)
        self.window.set_child(child)


class Card(Surface):
    """Progress of one piece of work: a header with the title and the phase it
    is in, a meter row led by its clock, and the text it is working on. Gone
    when its timeout runs out unless the next message arrives first."""

    def __init__(self, app: Gtk.Application):
        super().__init__(app, "overlay-card", OverlayPosition.BOTTOM_RIGHT)
        self.icon = Gtk.Image(icon_size=Gtk.IconSize.LARGE, valign=Gtk.Align.START)
        self.title = Gtk.Label(xalign=0, hexpand=True, css_classes=["heading"])
        self.status = Gtk.Label(
            xalign=1, attributes=self.medium(), css_classes=["caption", "dim-label"]
        )
        self.clock = Gtk.Label(css_classes=["numeric", "caption-heading"])
        self.level = Gtk.ProgressBar(hexpand=True, valign=Gtk.Align.CENTER)
        self.text = Gtk.Label(
            xalign=0,
            wrap=True,
            max_width_chars=40,
            width_chars=40,
            attributes=self.medium(),
        )

        header = Gtk.Box(spacing=12)
        header.append(self.title)
        header.append(self.status)
        self.meter = Gtk.Box(spacing=8)
        self.meter.append(self.clock)
        self.meter.append(self.level)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        body.append(header)
        body.append(self.meter)
        body.append(self.text)
        root = Gtk.Box(spacing=12)
        root.append(self.icon)
        root.append(body)
        self.frame(root)
        self._expiry = 0

    def show(
        self,
        title: str = "",
        icon: str = "",
        status: str = "",
        clock: str | None = None,
        level: float | None = None,
        text: str = "",
        extract: bool = False,
        timeout_ms: int = 1000,
        position: str | None = None,
    ):
        self.place(OverlayPosition(position or self.default))
        self.icon.set_from_icon_name(icon or None)
        self.icon.set_visible(bool(icon))
        self.title.set_label(title)
        self.status.set_label(status)
        self.status.set_visible(bool(status))
        self.clock.set_label(clock or "")
        self.clock.set_visible(clock is not None)
        self.level.set_fraction(level or 0.0)
        self.level.set_visible(level is not None)
        self.meter.set_visible(clock is not None or level is not None)
        self.text.set_label(text)
        self.text.set_visible(bool(text))
        # A clipped fragment of something longer reads quieter than a message.
        if extract:
            self.text.add_css_class("dim-label")
        else:
            self.text.remove_css_class("dim-label")
        if self._expiry:
            GLib.source_remove(self._expiry)
        self._expiry = GLib.timeout_add(timeout_ms, self._expire)
        self.window.set_visible(True)

    def hide(self) -> None:
        if self._expiry:
            GLib.source_remove(self._expiry)
            self._expiry = 0
        self.window.set_visible(False)

    def _expire(self) -> bool:
        self._expiry = 0
        self.window.set_visible(False)

        return GLib.SOURCE_REMOVE


class Hints(Surface):
    """What each key does in the active submap, read off the binds' own
    descriptions."""

    # Rows per column. The recording submap holds two dozen binds, and one
    # column of them would run off a laptop panel.
    ROWS = 12

    def __init__(self, app: Gtk.Application):
        super().__init__(app, "overlay-hints", OverlayPosition.CENTER)
        self.title = Gtk.Label(css_classes=["title-2"])
        self.grid = Gtk.Grid(column_spacing=12, row_spacing=6)
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        root.append(self.title)
        root.append(self.grid)
        self.frame(root)

    def show(
        self,
        title: str = "",
        rows: list[list[str]] | None = None,
        position: str | None = None,
    ):
        self.place(OverlayPosition(position or self.default))
        self.title.set_label(title)
        while child := self.grid.get_first_child():
            self.grid.remove(child)
        for i, (accelerators, description) in enumerate(rows or []):
            column, row = divmod(i, self.ROWS)
            self.grid.attach(
                Gtk.ShortcutLabel(accelerator=accelerators, halign=Gtk.Align.END),
                column * 2,
                row,
                1,
                1,
            )
            self.grid.attach(
                Gtk.Label(
                    label=description,
                    xalign=0,
                    margin_end=12,
                    attributes=self.medium(),
                ),
                column * 2 + 1,
                row,
                1,
                1,
            )
        self.window.set_visible(True)

    def hide(self):
        self.window.set_visible(False)


class Submaps:
    """Follows Hyprland's submap and turns the active one's binds into hint rows."""

    # Hyprland's modmask bits, as GTK accelerator modifiers.
    MODS: ClassVar[tuple[tuple[int, str], ...]] = (
        (64, "<Super>"),
        (4, "<Control>"),
        (8, "<Alt>"),
        (1, "<Shift>"),
    )
    RETRY_SECONDS = 2.0

    log = logging.getLogger("submaps")

    def __init__(self, signature: str):
        self.dir = Path(os.environ["XDG_RUNTIME_DIR"]) / "hypr" / signature

    def binds(self) -> list[dict[str, Any]]:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(2.0)
            sock.connect(str(self.dir / ".socket.sock"))
            sock.sendall(b"j/binds")
            data = b""
            while chunk := sock.recv(65536):
                data += chunk

        return json.loads(data)

    def accelerator(self, bind: dict[str, Any]) -> str:
        """The bind as a GTK accelerator, for `Gtk.ShortcutLabel` to draw as
        theme keycaps. Hyprland keeps the key as written in the config, and
        GDK's names for the named keys are capitalised."""
        key = bind["key"]
        if len(key) > 1:
            key = key[0].upper() + key[1:]

        return "".join(name for bit, name in self.MODS if bind["modmask"] & bit) + key

    def rows(self, submap: str) -> list[list[str]]:
        """Binds sharing a description collapse into one row, so `Left` and
        `h` read as a single hint."""
        grouped: dict[str, list[str]] = {}
        for bind in self.binds():
            if bind["submap"] == submap and bind["has_description"]:
                grouped.setdefault(bind["description"], []).append(
                    self.accelerator(bind)
                )

        return [[" ".join(keys), description] for description, keys in grouped.items()]

    def follow(self, hints: Hints) -> None:
        while True:
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                    sock.connect(str(self.dir / ".socket2.sock"))
                    self.log.info("following %s", self.dir)
                    for line in sock.makefile("r", encoding="utf-8"):
                        event, _, submap = line.rstrip("\n").partition(">>")
                        if event != "submap":
                            continue
                        if not submap:
                            GLib.idle_add(hints.hide)
                            continue
                        # A bad answer for one submap must not end the watch,
                        # or every later reset is missed and hints stay up.
                        try:
                            rows = self.rows(submap)
                        except Exception:
                            self.log.exception("binds for %s", submap)
                            rows = []
                        GLib.idle_add(hints.show, submap, rows)
            except OSError as e:
                self.log.warning("hyprland socket: %s", e)
            GLib.idle_add(hints.hide)
            time.sleep(self.RETRY_SECONDS)


class OverlayServer:
    log = logging.getLogger("overlay")

    def __init__(self):
        self.app = Gtk.Application(
            application_id="dev.kilic.overlay", flags=Gio.ApplicationFlags.NON_UNIQUE
        )
        self.app.connect("activate", self.activate)
        self.slots: dict[OverlaySlot, Card | Hints] = {}

    def activate(self, app: Gtk.Application) -> None:
        self.slots = {OverlaySlot.CARD: Card(app), OverlaySlot.HINTS: Hints(app)}
        app.hold()

        threading.Thread(target=self.listen, daemon=True).start()
        if signature := os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
            threading.Thread(
                target=Submaps(signature).follow,
                args=(self.slots[OverlaySlot.HINTS],),
                daemon=True,
            ).start()
        else:
            self.log.warning("no HYPRLAND_INSTANCE_SIGNATURE: submap hints disabled")

    def dispatch(self, message: dict[str, Any]) -> bool:
        try:
            slot = self.slots[OverlaySlot(message.pop("slot"))]
            match OverlayOp(message.pop("op")):
                case OverlayOp.SHOW:
                    slot.show(**message)
                case OverlayOp.HIDE:
                    slot.hide()
        except (KeyError, ValueError, TypeError) as e:
            self.log.warning("bad message %r: %s", message, e)

        return GLib.SOURCE_REMOVE

    def serve(self, conn: socket.socket) -> None:
        with conn, conn.makefile("r", encoding="utf-8") as lines:
            for line in lines:
                try:
                    GLib.idle_add(self.dispatch, json.loads(line))
                except json.JSONDecodeError as e:
                    self.log.warning("bad line %r: %s", line, e)

    def listen(self) -> None:
        path = Overlay.SOCKET
        path.unlink(missing_ok=True)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(path))
            server.listen()
            self.log.info("listening on %s", path)
            while True:
                conn, _ = server.accept()
                threading.Thread(target=self.serve, args=(conn,), daemon=True).start()

    # ── CLI ───────────────────────────────────────────────────────

    @click.group(context_settings={"help_option_names": ["-h", "--help"]})
    @click.option("-v", "--verbose", is_flag=True, help="Enable debug logging.")
    def cli(verbose: bool):
        """On-screen cards and submap hints, drawn on a layer-shell surface."""
        create_logger(verbose, log_file="overlay.log")

    @cli.command("serve")
    def cmd_serve():
        """Run the overlay daemon."""
        if not LayerShell.is_supported():
            raise click.ClickException(
                "layer shell unavailable: is this a wayland session?"
            )
        OverlayServer().app.run([])


if __name__ == "__main__":
    OverlayServer.cli()
