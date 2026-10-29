#!/usr/bin/env -S sh -c 'd="$(dirname "$0")"; uv sync -q --project "$d" && exec "$d/.venv/bin/python" "$0" "$@"'
"""Create and remove virtual monitors at an arbitrary mode.

`start` creates a headless output, sets it to the requested mode and focuses
it; `stop` removes it. A width, height or fps left unset falls back to
Sunshine's `SUNSHINE_CLIENT_*` variables, which it exports to an app's prep
commands, so a stream gets a native frame at the client's own resolution
whatever the physical monitors offer.

Headless outputs carry no description, so the monitor profiles never sweep
one into an `ANY` disable rule.

`--reveal` lifts the `no_screen_share` rules `screen-share.lua` keeps, so a
remote desktop stream shows the windows a screen share hides; that display's
`stop` puts them back."""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

import click
from dotlib.cli import create_logger
from dotlib.waybar import watch
from lib import Hyprctl


class VirtualDisplay:
    DEFAULT_NAME = "VIRTUAL"
    DEFAULT_WIDTH = 1920
    DEFAULT_HEIGHT = 1080
    DEFAULT_FPS = 60

    log = logging.getLogger("virtual-display")

    def __init__(self, hypr: Hyprctl, name: str):
        self._hypr = hypr
        self.name = name
        # Marks that this display lifted the hiding, so only its own stop puts
        # it back: stopping another virtual display must not re-hide windows
        # in a stream that is still running.
        self._revealed = (
            Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
            / f"virtual-display-{name}.revealed"
        )

    def monitor(self) -> dict | None:
        for m in self._hypr.query("monitors", "all") or []:
            if m.get("name") == self.name:
                return m

        return None

    def exists(self) -> bool:
        return self.monitor() is not None

    def status_json(self) -> str:
        monitor = self.monitor()
        # The empty text hides the waybar module while no virtual monitor exists.
        if monitor is None:
            return json.dumps({"class": "idle", "text": "", "tooltip": ""})

        return json.dumps(
            {
                "class": "active",
                "text": "󰢹",
                "tooltip": (
                    f"Remote session on {self.name} "
                    f"({monitor['width']}x{monitor['height']} @ {monitor['scale']:g}x)"
                ),
            }
        )

    def screen_share_hidden(self, hidden: bool) -> None:
        self.log.info("screen share hiding %s", "on" if hidden else "off")
        self._hypr.eval(f'require("screen-share").set({str(hidden).lower()})')

    def start(
        self, width: int, height: int, fps: int, scale: float, reveal: bool
    ) -> None:
        if not self.exists():
            self.log.info("creating headless output %s", self.name)
            self._hypr.output("create", "headless", self.name)
        self.log.info(
            "mode %dx%d@%d scale %g on %s", width, height, fps, scale, self.name
        )
        self._hypr.eval(
            f'hl.monitor({{ output = "{self.name}", mode = "{width}x{height}@{fps}", position = "auto", scale = "{scale:g}" }})'
        )
        self._hypr.dispatch(f'hl.dsp.focus({{ monitor = "{self.name}" }})')
        if reveal:
            self.screen_share_hidden(False)
            self._revealed.touch()

    def stop(self) -> None:
        if self._revealed.exists():
            self.screen_share_hidden(True)
            self._revealed.unlink()
        if not self.exists():
            self.log.info("%s already gone", self.name)
            return
        self.log.info("removing headless output %s", self.name)
        self._hypr.output("remove", self.name)

    # ── CLI ───────────────────────────────────────────────────────

    @staticmethod
    def client_value(key: str, default: int) -> int:
        value = os.environ.get(f"SUNSHINE_CLIENT_{key}")

        return int(value) if value else default

    @staticmethod
    def client_scale(scales: tuple[str, ...], default: float) -> float:
        """The scale for SUNSHINE_CLIENT_NAME out of `NAME=SCALE` pairs."""
        client = os.environ.get("SUNSHINE_CLIENT_NAME")
        for pair in scales:
            name, _, scale = pair.partition("=")
            if name == client:
                return float(scale)

        return default

    @click.group(context_settings={"help_option_names": ["-h", "--help"]})
    @click.option("-v", "--verbose", is_flag=True, help="Enable debug logging.")
    def cli(verbose: bool):
        """Manage headless virtual monitors."""
        create_logger(verbose, log_file="virtual-display.log", quiet={"status"})

    @cli.command("start")
    @click.option("-n", "--name", default=DEFAULT_NAME, show_default=True)
    @click.option("--width", type=int, help="Defaults to SUNSHINE_CLIENT_WIDTH.")
    @click.option("--height", type=int, help="Defaults to SUNSHINE_CLIENT_HEIGHT.")
    @click.option("--fps", type=int, help="Defaults to SUNSHINE_CLIENT_FPS.")
    @click.option("--scale", type=float, default=1.0, show_default=True)
    @click.option(
        "--client-scale",
        "client_scales",
        multiple=True,
        metavar="NAME=SCALE",
        help="Scale for the client named in SUNSHINE_CLIENT_NAME; repeatable.",
    )
    @click.option(
        "--reveal",
        is_flag=True,
        help="Show windows that screen shares hide, until stop.",
    )
    def cmd_start(
        name: str,
        width: int | None,
        height: int | None,
        fps: int | None,
        scale: float,
        client_scales: tuple[str, ...],
        reveal: bool,
    ):
        """Create the virtual monitor at the given mode and focus it."""
        cls = VirtualDisplay
        VirtualDisplay(Hyprctl(), name).start(
            width or cls.client_value("WIDTH", cls.DEFAULT_WIDTH),
            height or cls.client_value("HEIGHT", cls.DEFAULT_HEIGHT),
            fps or cls.client_value("FPS", cls.DEFAULT_FPS),
            cls.client_scale(client_scales, scale),
            reveal,
        )

    @cli.command("stop")
    @click.option("-n", "--name", default=DEFAULT_NAME, show_default=True)
    def cmd_stop(name: str):
        """Remove the virtual monitor."""
        VirtualDisplay(Hyprctl(), name).stop()

    @cli.command("status")
    @click.option("-n", "--name", default=DEFAULT_NAME, show_default=True)
    @click.option(
        "--watch", "watching", is_flag=True, help="Print a line per change, for waybar."
    )
    def cmd_status(name: str, watching: bool):
        """Print waybar-shaped status JSON."""
        if watching:
            return watch(VirtualDisplay(Hyprctl(), name).status_json, 1.0)
        sys.stdout.write(VirtualDisplay(Hyprctl(), name).status_json() + "\n")


if __name__ == "__main__":
    sys.exit(VirtualDisplay.cli())
