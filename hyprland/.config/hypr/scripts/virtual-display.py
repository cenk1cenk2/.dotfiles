#!/usr/bin/env -S sh -c 'd="$(dirname "$0")"; uv sync -q --project "$d" && exec "$d/.venv/bin/python" "$0" "$@"'
"""Create and remove virtual monitors at an arbitrary mode.

`start` creates a headless output, sets it to the requested mode and focuses
it; `stop` removes it. A width, height or fps left unset falls back to
Sunshine's `SUNSHINE_CLIENT_*` variables, which it exports to an app's prep
commands, so a stream gets a native frame at the client's own resolution
whatever the physical monitors offer.

Headless outputs carry no description, so the monitor profiles never sweep
one into an `ANY` disable rule."""

from __future__ import annotations

import logging
import os
import sys

import click
from dotlib.cli import create_logger
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

    def exists(self) -> bool:
        return any(
            m.get("name") == self.name
            for m in self._hypr.query("monitors", "all") or []
        )

    def start(self, width: int, height: int, fps: int) -> None:
        if not self.exists():
            self.log.info("creating headless output %s", self.name)
            self._hypr.output("create", "headless", self.name)
        self.log.info("mode %dx%d@%d on %s", width, height, fps, self.name)
        self._hypr.eval(
            f'hl.monitor({{ output = "{self.name}", mode = "{width}x{height}@{fps}", position = "auto", scale = "1" }})'
        )
        self._hypr.dispatch(f'hl.dsp.focus({{ monitor = "{self.name}" }})')

    def stop(self) -> None:
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

    @click.group(context_settings={"help_option_names": ["-h", "--help"]})
    @click.option("-v", "--verbose", is_flag=True, help="Enable debug logging.")
    def cli(verbose: bool):
        """Manage headless virtual monitors."""
        create_logger(verbose, log_file="virtual-display.log")

    @cli.command("start")
    @click.option("-n", "--name", default=DEFAULT_NAME, show_default=True)
    @click.option("--width", type=int, help="Defaults to SUNSHINE_CLIENT_WIDTH.")
    @click.option("--height", type=int, help="Defaults to SUNSHINE_CLIENT_HEIGHT.")
    @click.option("--fps", type=int, help="Defaults to SUNSHINE_CLIENT_FPS.")
    def cmd_start(name: str, width: int | None, height: int | None, fps: int | None):
        """Create the virtual monitor at the given mode and focus it."""
        cls = VirtualDisplay
        VirtualDisplay(Hyprctl(), name).start(
            width or cls.client_value("WIDTH", cls.DEFAULT_WIDTH),
            height or cls.client_value("HEIGHT", cls.DEFAULT_HEIGHT),
            fps or cls.client_value("FPS", cls.DEFAULT_FPS),
        )

    @cli.command("stop")
    @click.option("-n", "--name", default=DEFAULT_NAME, show_default=True)
    def cmd_stop(name: str):
        """Remove the virtual monitor."""
        VirtualDisplay(Hyprctl(), name).stop()


if __name__ == "__main__":
    sys.exit(VirtualDisplay.cli())
