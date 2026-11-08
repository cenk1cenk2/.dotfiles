#!/usr/bin/env -S sh -c 'd="$(dirname "$0")"; uv sync -q --project "$d" && exec "$d/.venv/bin/python" "$0" "$@"'
"""Switch to or steal a window from any workspace via rofi."""

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


class Window:
    log = logging.getLogger("window")
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
    )
    ROFI_CLOSE = 10

    def __init__(self, hypr: Hyprctl, style: Style):
        self._hypr = hypr
        self._style = style

    def switch(self) -> None:
        # Most recently focused first, with the previous window preselected,
        # as Windows orders Alt-Tab.
        window = self._choose("Switch window", self._hypr.clients(), row=1)
        if window is None:
            return

        self._focus(window)
        self.log.info("Switched to window: %s", window.get("title", "Untitled"))

    def steal(self) -> None:
        active = self._hypr.active_workspace()
        if not active:
            raise click.ClickException("No active workspace")

        window = self._choose(
            "Steal window",
            [
                w
                for w in self._hypr.clients()
                if w.get("workspace", {}).get("id") != active["id"]
            ],
            row=0,
        )
        if window is None:
            return

        self._hypr.dispatch(
            f'hl.dsp.window.move({{ workspace = "{active["id"]}", window = "address:{window["address"]}" }})'
        )
        self._focus(window)
        self.log.info("Stole window: %s", window.get("title", "Untitled"))

    def _choose(self, prompt: str, windows: list[dict], row: int) -> dict | None:
        """Pick from windows by focus order, closing the pick on request."""
        if not windows:
            self.log.info("No windows available")
            return None

        windows.sort(key=lambda w: w.get("focusHistoryID", 999))
        selected, close = self._pick(prompt, windows, row)
        if selected is None or selected >= len(windows):
            return None

        window = windows[selected]
        if close:
            self._hypr.dispatch(
                f'hl.dsp.window.close({{ window = "address:{window["address"]}" }})'
            )
            self.log.info("Closed window: %s", window.get("title", "Untitled"))
            return None

        return window

    def _focus(self, window: dict) -> None:
        self._hypr.dispatch(
            f'hl.dsp.focus({{ window = "address:{window["address"]}" }})'
        )
        self._hypr.dispatch(
            f'hl.dsp.window.alter_zorder({{ mode = "top", window = "address:{window["address"]}" }})'
        )

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

    def _pick(
        self, prompt: str, windows: list[dict], row: int
    ) -> tuple[int | None, bool]:
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
            prompt,
            "-format",
            "i",
            "-show-icons",
            "-async-pre-read",
            "0",
            *self.ROFI_ARGS,
            "-selected-row",
            str(row),
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
            match self._style:
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

    @click.group(context_settings={"help_option_names": ["-h", "--help"]})
    @click.option(
        "-s",
        "--style",
        type=click.Choice([s.value for s in Style]),
        default=Style.ICON.value,
        show_default=True,
        help="Tile content: app icons or window captures.",
    )
    @click.option("-v", "--verbose", is_flag=True, help="Show capture traces.")
    @click.pass_context
    def cli(ctx: click.Context, style: str, verbose: bool) -> None:
        """Switch to or steal a window."""
        create_logger(verbose, name="window")
        ctx.obj = Style(style)

    @cli.command("switch")
    @click.pass_obj
    def cmd_switch(style: Style) -> None:
        """Focus and raise a window from any workspace."""
        Window(Hyprctl(), style).switch()

    @cli.command("steal")
    @click.pass_obj
    def cmd_steal(style: Style) -> None:
        """Move a window here from another workspace, then focus and raise it."""
        Window(Hyprctl(), style).steal()


if __name__ == "__main__":
    Window.cli()
