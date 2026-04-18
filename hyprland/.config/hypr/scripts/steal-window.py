#!/usr/bin/env python3
"""Steal a window from another workspace into the current one via rofi."""

import sys

from lib import Hyprctl, get_icon_for_class, rofi_with_icons

ROFI_EXTRA = [
    "-theme-str", "window { width: 60%; }",
    "-theme-str", "listview { lines: 15; }",
]

class StealWindow:
    def __init__(self, args, hypr: Hyprctl):
        self.args = args
        self._hypr = hypr

    def run(self):
        current_ws = self._current_workspace_id()
        elsewhere = self._windows_on_other_workspaces(current_ws)
        if not elsewhere:
            print("No windows available to steal")
            return

        entries = [
            (self._format(w), get_icon_for_class(w.get("class", "Unknown")))
            for w in elsewhere
        ]
        selected = rofi_with_icons("Steal window", entries, extra_args=ROFI_EXTRA)
        if selected is None or selected >= len(elsewhere):
            return

        window = elsewhere[selected]
        self._hypr.dispatch(
            "movetoworkspace",
            f"{current_ws},address:{window['address']}",
        )
        print(f"Stole window: {window.get('title', 'Untitled')}")

    def _current_workspace_id(self) -> int:
        active = self._hypr.active_workspace()
        if not active:
            print("No active workspace", file=sys.stderr)
            sys.exit(1)

        return active["id"]

    def _windows_on_other_workspaces(self, current_ws: int) -> list[dict]:
        return sorted(
            (
                w for w in self._hypr.clients()
                if w.get("workspace", {}).get("id") != current_ws
            ),
            key=lambda w: w.get("workspace", {}).get("id", 0),
        )

    @staticmethod
    def _format(window: dict) -> str:
        title = window.get("title", "Untitled")
        class_name = window.get("class", "Unknown")
        workspace = window.get("workspace", {}).get("id", "?")

        return f"[{workspace}] {title} - {class_name}"

def main():
    StealWindow(args=None, hypr=Hyprctl()).run()

if __name__ == "__main__":
    main()
