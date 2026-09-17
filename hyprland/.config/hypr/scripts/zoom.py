#!/usr/bin/env -S sh -c 'exec uv run --project "$(dirname "$0")" "$0" "$@"'
"""Toggle the compositor's own cursor zoom.

Hyprland animates `cursor:zoom_factor` itself through the `zoomFactor`
animation leaf, so there is nothing to step here and no state to keep: the
compositor is asked for the current factor and told the next one."""

from __future__ import annotations

import json
import logging
import sys

import click
from dotlib.cli import (
    create_logger,
)
from dotlib.waybar import (
    signal_waybar,
)
from lib import Hyprctl


class Zoom:
    WAYBAR_MODULE = "zoom"
    OPTION = "cursor:zoom_factor"
    DEFAULT_TARGET = 1.5
    MIN_FACTOR = 1.0
    MAX_FACTOR = 10.0
    # The factor round-trips through Lua and JSON as a float, so "unzoomed"
    # is a neighbourhood of 1.0 rather than the value itself.
    EPSILON = 1e-3

    log = logging.getLogger("zoom")

    def __init__(self, hypr: Hyprctl):
        self._hypr = hypr

    def factor(self) -> float:
        option = self._hypr.query("getoption", self.OPTION)
        if not option:
            self.log.debug("no factor read from %s", self.OPTION)
            return self.MIN_FACTOR

        return float(option.get("float", self.MIN_FACTOR))

    def is_zoomed(self) -> bool:
        return self.factor() > self.MIN_FACTOR + self.EPSILON

    def apply(self, factor: float) -> None:
        """Write the factor, clamped, and poke waybar.

        `hyprctl keyword` is dead under the Lua config manager, so a runtime
        write is a Lua expression the compositor evaluates."""
        factor = min(max(factor, self.MIN_FACTOR), self.MAX_FACTOR)
        self.log.info("zoom factor: %g", factor)
        self._hypr.eval(f"hl.config({{ cursor = {{ zoom_factor = {factor:g} }} }})")
        signal_waybar(self.WAYBAR_MODULE)

    def toggle(self, target: float) -> None:
        self.apply(self.MIN_FACTOR if self.is_zoomed() else target)

    def status_json(self) -> str:
        factor = self.factor()
        # Only reachable with the module's exec-if dropped: waybar hides the
        # module entirely while `is-zoomed` fails.
        if factor <= self.MIN_FACTOR + self.EPSILON:
            return json.dumps(
                {"class": "idle", "text": "", "tooltip": "Cursor zoom off"}
            )

        return json.dumps(
            {
                "class": "zoomed",
                "text": f"󰍉 {factor:g}×",
                "tooltip": f"Cursor zoom {factor:g}x - Click to reset",
            }
        )

    # ── CLI ───────────────────────────────────────────────────────

    @click.group(context_settings={"help_option_names": ["-h", "--help"]})
    @click.option("-v", "--verbose", is_flag=True, help="Enable debug logging.")
    def cli(verbose: bool):
        """Control the compositor's cursor zoom."""
        create_logger(verbose, log_file="zoom.log", quiet={"status", "is-zoomed"})

    @cli.command("toggle")
    @click.option(
        "-t",
        "--target",
        type=float,
        default=DEFAULT_TARGET,
        help="Factor to zoom into.",
    )
    def cmd_toggle(target: float):
        """Toggle between 1x and the target factor."""
        Zoom(Hyprctl()).toggle(target)

    @cli.command("set")
    @click.argument("factor", type=float)
    def cmd_set(factor: float):
        """Set the zoom factor, clamped to 1-10."""
        Zoom(Hyprctl()).apply(factor)

    @cli.command("reset")
    def cmd_reset():
        """Zoom back out to 1x."""
        Zoom(Hyprctl()).apply(Zoom.MIN_FACTOR)

    @cli.command("status")
    def cmd_status():
        """Print waybar-shaped status JSON."""
        sys.stdout.write(Zoom(Hyprctl()).status_json() + "\n")

    @cli.command("is-zoomed")
    def cmd_is_zoomed():
        """Exit 0 if the cursor is zoomed in."""
        sys.exit(0 if Zoom(Hyprctl()).is_zoomed() else 1)


if __name__ == "__main__":
    Zoom.cli()
