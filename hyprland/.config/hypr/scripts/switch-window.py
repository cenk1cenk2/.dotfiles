#!/usr/bin/env python3
"""Switch to any window from any workspace via rofi.

Current workspace first, then others; the focused window is marked.
"""

from lib import Hyprctl, get_icon_for_class, rofi_with_icons

ROFI_EXTRA = [
    "-theme-str", "window { width: 60%; }",
    "-theme-str", "listview { lines: 15; }",
]

class SwitchWindow:
    def __init__(self, args, hypr: Hyprctl):
        self.args = args
        self._hypr = hypr

    def run(self):
        windows = self._hypr.clients()
        if not windows:
            print("No windows available")
            return

        focused = self._hypr.active_window() or {}
        focused_addr = focused.get("address", "")
        current_ws = (self._hypr.active_workspace() or {}).get("id", 0)

        windows.sort(key=lambda w: self._sort_key(w, current_ws, focused_addr))

        entries = [
            (
                self._format(w, w.get("address") == focused_addr),
                get_icon_for_class(w.get("class", "Unknown")),
            )
            for w in windows
        ]
        selected = rofi_with_icons("Switch window", entries, extra_args=ROFI_EXTRA)
        if selected is None or selected >= len(windows):
            return

        window = windows[selected]
        self._hypr.dispatch("focuswindow", f"address:{window['address']}")
        print(f"Switched to window: {window.get('title', 'Untitled')}")

    @staticmethod
    def _sort_key(window: dict, current_ws: int, focused_addr: str):
        ws_id = window.get("workspace", {}).get("id", 999)

        return (
            0 if ws_id == current_ws else 1,
            0 if window.get("address") == focused_addr else 1,
            ws_id,
        )

    @staticmethod
    def _format(window: dict, focused: bool) -> str:
        title = window.get("title", "Untitled")
        if len(title) > 60:
            title = title[:57] + "..."
        class_name = window.get("class", "Unknown")
        workspace = window.get("workspace", {}).get("id", "?")
        marker = "● " if focused else "  "

        return f"{marker}[WS {workspace}] {title} - {class_name}"

def main():
    SwitchWindow(args=None, hypr=Hyprctl()).run()

if __name__ == "__main__":
    main()
