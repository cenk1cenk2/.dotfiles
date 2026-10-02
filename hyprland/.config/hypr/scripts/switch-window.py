#!/usr/bin/env -S sh -c 'exec uv run --project "$(dirname "$0")" "$0" "$@"'
"""Switch to any window from any workspace via rofi."""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from enum import StrEnum
from pathlib import Path

import click
from dotlib.cli import create_logger

from lib import Hyprctl, get_icon_for_class, rofi_with_icons


class Style(StrEnum):
    TEXT = "text"
    SCREENSHOT = "screenshot"


class SwitchWindow:
    log = logging.getLogger("switch-window")
    RUNTIME_DIR = os.environ.get("XDG_RUNTIME_DIR", "/tmp")

    TEXT_ROFI = (
        "-theme-str",
        "window { width: 60%; }",
        "-theme-str",
        "listview { lines: 15; }",
    )
    # rofi sees Tab with the held modifier, hence the Super+ variants. Custom
    # key 1 closes the selected window instead, which rofi reports as exit
    # code 10.
    SCREENSHOT_ROFI = (
        "-theme-str",
        "window { width: 70%; }",
        "-theme-str",
        "listview { columns: 4; lines: 3; flow: horizontal; }",
        "-theme-str",
        "element { orientation: vertical; }",
        "-theme-str",
        "element-icon { size: 10em; }",
        "-theme-str",
        "element-text { horizontal-align: 0.5; }",
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
        close = False
        windows = self._hypr.clients()
        if not windows:
            self.log.info("No windows available")
            return

        match style:
            case Style.TEXT:
                focused = (self._hypr.active_window() or {}).get("address", "")
                current_ws = (self._hypr.active_workspace() or {}).get("id", 0)
                windows.sort(key=lambda w: self._sort_key(w, current_ws, focused))
                entries = [
                    (
                        self._format_text(w, w.get("address") == focused),
                        get_icon_for_class(w.get("class", "Unknown")),
                    )
                    for w in windows
                ]
                selected = rofi_with_icons(
                    "Switch window", entries, extra_args=list(self.TEXT_ROFI)
                )
            case Style.SCREENSHOT:
                # Most recently focused first, with the previous window
                # preselected, as Windows orders Alt-Tab.
                windows.sort(key=lambda w: w.get("focusHistoryID", 999))
                selected, close = self._pick_streamed(windows)

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

    def _pick_streamed(self, windows: list[dict]) -> tuple[int | None, bool]:
        """Open rofi at once and feed it each tile as its capture lands.

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
            *self.SCREENSHOT_ROFI,
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
            captures = [
                pool.submit(self._thumbnail, w, Path(directory)) for w in windows
            ]
            try:
                for window, capture in zip(windows, captures):
                    label = self._format_tile(window, window.get("focusHistoryID") == 0)
                    rofi.stdin.write(f"{label}\x00icon\x1f{capture.result()}\n")
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
        cmd = ["grim", "-T", stable_id, "-s", "0.15", "-t", "jpeg", str(path)]
        self.log.debug("spawn: %s", " ".join(cmd))
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=2, check=False)
        except subprocess.TimeoutExpired:
            return icon
        self.log.debug("grim stderr: %s", proc.stderr.decode(errors="replace"))

        return str(path) if proc.returncode == 0 else icon

    @staticmethod
    def _sort_key(window: dict, current_ws: int, focused_addr: str):
        ws_id = window.get("workspace", {}).get("id", 999)

        return (
            0 if ws_id == current_ws else 1,
            0 if window.get("address") == focused_addr else 1,
            ws_id,
        )

    @staticmethod
    def _format_text(window: dict, focused: bool) -> str:
        title = window.get("title", "Untitled")
        if len(title) > 60:
            title = title[:57] + "..."
        class_name = window.get("class", "Unknown")
        workspace = window.get("workspace", {}).get("id", "?")
        marker = "● " if focused else "  "

        return f"{marker}[WS {workspace}] {title} - {class_name}"

    @staticmethod
    def _format_tile(window: dict, focused: bool) -> str:
        title = window.get("title", "Untitled")
        if len(title) > 40:
            title = title[:37] + "..."
        workspace = window.get("workspace", {}).get("id", "?")
        marker = "● " if focused else ""

        return f"{marker}[{workspace}] {title}"


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "-s",
    "--style",
    type=click.Choice([s.value for s in Style]),
    default=Style.SCREENSHOT.value,
    show_default=True,
    help="Switcher layout.",
)
@click.option("-v", "--verbose", is_flag=True, help="Show capture traces.")
def cmd_main(style: str, verbose: bool) -> None:
    create_logger(verbose, name="switch-window")
    SwitchWindow(Hyprctl()).run(Style(style))


SwitchWindow.cli = cmd_main

if __name__ == "__main__":
    SwitchWindow.cli()
