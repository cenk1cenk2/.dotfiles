#!/usr/bin/env -S sh -c 'exec uv run --project "$(dirname "$0")" "$0" "$@"'
"""Switch to any window from any workspace via rofi."""

from __future__ import annotations

import logging
import math
import os
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from enum import StrEnum
from pathlib import Path

import click
from dotlib.cli import create_logger

from lib import Hyprctl, get_icon_for_class, get_name_for_class


class Style(StrEnum):
    ICON = "icon"
    SCREENSHOT = "screenshot"


class SwitchWindow:
    log = logging.getLogger("switch-window")
    RUNTIME_DIR = os.environ.get("XDG_RUNTIME_DIR", "/tmp")

    WIDTH = 0.75
    HEIGHT = 0.85
    COLUMNS = 3
    MAX_ROWS = 3
    # Logical px rofi spends outside the icons: per row the label, element
    # padding and spacing; per window the prompt and mainbox padding.
    ROW_CHROME = 70
    WINDOW_CHROME = 110

    # rofi sees Tab with the held modifier, hence the Super+ variants. Custom
    # key 1 closes the selected window instead, which rofi reports as exit
    # code 10.
    ROFI_ARGS = (
        "-theme-str",
        "mainbox { padding: 1em; }",
        "-theme-str",
        "element { orientation: vertical; }",
        "-theme-str",
        "element-text { horizontal-align: 0.5; expand: false; }",
        "-kb-element-next",
        "Tab,Super+Tab",
        "-kb-element-prev",
        "ISO_Left_Tab,Super+ISO_Left_Tab",
        "-kb-accept-entry",
        "Return,KP_Enter",
        "-kb-custom-1",
        "Control+x",
        "-selected-row",
        "1",
    )
    ROFI_CLOSE = 10

    def __init__(self, hypr: Hyprctl):
        self._hypr = hypr

    def run(self, style: Style) -> None:
        windows = self._hypr.clients()
        if not windows:
            self.log.info("No windows available")
            return

        # Most recently focused first, with the previous window preselected,
        # as Windows orders Alt-Tab.
        windows.sort(key=lambda w: w.get("focusHistoryID", 999))
        selected, close = self._pick(windows, style)
        if selected is None or selected >= len(windows):
            return

        window = windows[selected]
        if close:
            self._hypr.dispatch(
                f'hl.dsp.window.close({{ window = "address:{window["address"]}" }})'
            )
            self.log.info("Closed window: %s", window.get("title", "Untitled"))
            return

        self._hypr.dispatch(
            f'hl.dsp.focus({{ window = "address:{window["address"]}" }})'
        )
        self.log.info("Switched to window: %s", window.get("title", "Untitled"))

    def _grid_theme(self, count: int) -> list[str]:
        """Size the grid to the focused monitor so tiles fill the window width.

        rofi sizes icons only in absolute units and derives row height from
        that size, so the fit is computed here: each tile is as wide as its
        column, shrunk until every row fits the screen height, and the window
        collapses to the rows actually used.
        """
        monitor = self._hypr.focused_monitor() or {}
        scale = monitor.get("scale", 1) or 1
        width = monitor.get("width", 1920) / scale
        height = monitor.get("height", 1080) / scale
        if monitor.get("transform", 0) % 2:
            width, height = height, width

        rows = min(self.MAX_ROWS, math.ceil(count / self.COLUMNS))
        column = (width * self.WIDTH - 2 * self.ROW_CHROME) / self.COLUMNS
        per_row = (height * self.HEIGHT - self.WINDOW_CHROME) / rows - self.ROW_CHROME
        size = int(min(column, per_row))
        window_height = int(rows * (size + self.ROW_CHROME) + self.WINDOW_CHROME)

        return [
            "-theme-str",
            f"window {{ width: {int(width * self.WIDTH)}px; height: {window_height}px; }}",
            "-theme-str",
            (
                f"listview {{ columns: {self.COLUMNS}; lines: {rows}; flow: horizontal; "
                "fixed-height: true; }"
            ),
            "-theme-str",
            f"element-icon {{ size: {size}px; expand: true; }}",
        ]

    def _pick(self, windows: list[dict], style: Style) -> tuple[int | None, bool]:
        """Open rofi at once and feed it each tile as its image is ready.

        rofi cannot swap an image once drawn, so tiles are written in focus
        order rather than updated; a pick made before the rest arrive closes
        the pipe, which ends the feed. Captures live in a per-run directory
        removed once rofi has exited, so closed windows leave nothing behind.
        """
        cmd = [
            "rofi",
            "-dmenu",
            "-i",
            "-p",
            "Switch window",
            "-format",
            "i",
            "-show-icons",
            "-async-pre-read",
            "0",
            *self.ROFI_ARGS,
            *self._grid_theme(len(windows)),
        ]
        self.log.debug("spawn: %s", " ".join(cmd))
        rofi = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
        )
        with (
            tempfile.TemporaryDirectory(
                dir=self.RUNTIME_DIR, prefix="switch-window-"
            ) as directory,
            ThreadPoolExecutor(max_workers=len(windows)) as pool,
        ):
            match style:
                case Style.SCREENSHOT:
                    images = [
                        pool.submit(self._thumbnail, w, Path(directory))
                        for w in windows
                    ]
                case Style.ICON:
                    images = [
                        pool.submit(get_icon_for_class, w.get("class", "Unknown"))
                        for w in windows
                    ]
            try:
                for window, image in zip(windows, images):
                    label = self._format_tile(window, window.get("focusHistoryID") == 0)
                    rofi.stdin.write(f"{label}\x00icon\x1f{image.result()}\n")
                    rofi.stdin.flush()
                rofi.stdin.close()
            except BrokenPipeError:
                pass
            out = rofi.stdout.read()
            rofi.wait()
        if rofi.returncode not in (0, self.ROFI_CLOSE):
            return None, False

        try:
            return int(out.strip()), rofi.returncode == self.ROFI_CLOSE
        except ValueError:
            return None, False

    def _thumbnail(self, window: dict, directory: Path) -> str:
        """Capture the window by its toplevel id, falling back to its app icon.

        grim -T captures hidden windows and other workspaces too; Chromium apps
        that stopped drawing while hidden come out black or stale.
        """
        icon = get_icon_for_class(window.get("class", "Unknown"))
        stable_id = str(window.get("stableId", ""))
        if not stable_id:
            return icon

        path = directory / f"{stable_id}.jpg"
        cmd = ["grim", "-T", stable_id, "-s", "0.25", "-t", "jpeg", str(path)]
        self.log.debug("spawn: %s", " ".join(cmd))
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=2, check=False)
        except subprocess.TimeoutExpired:
            return icon
        self.log.debug("grim stderr: %s", proc.stderr.decode(errors="replace"))

        return str(path) if proc.returncode == 0 else icon

    @staticmethod
    def _format_tile(window: dict, focused: bool) -> str:
        title = window.get("title", "Untitled")
        if len(title) > 40:
            title = title[:37] + "..."
        app = get_name_for_class(window.get("class", "Unknown"))
        workspace = window.get("workspace", {}).get("id", "?")
        marker = "● " if focused else ""

        return f"{marker}[{workspace}] {app} - {title}"


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "-s",
    "--style",
    type=click.Choice([s.value for s in Style]),
    default=Style.ICON.value,
    show_default=True,
    help="Tile content: app icons or window captures.",
)
@click.option("-v", "--verbose", is_flag=True, help="Show capture traces.")
def cmd_main(style: str, verbose: bool) -> None:
    create_logger(verbose, name="switch-window")
    SwitchWindow(Hyprctl()).run(Style(style))


SwitchWindow.cli = cmd_main

if __name__ == "__main__":
    SwitchWindow.cli()
