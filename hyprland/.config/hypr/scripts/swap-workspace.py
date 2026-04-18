#!/usr/bin/env python3
"""Swap workspace positions in Hyprland by moving all windows between them.

- `-t/--to N`: move current workspace to N (swaps with existing windows).
- `-s/--swap left|right`: swap with the previous/next workspace on the
  current monitor.
"""

import sys
from argparse import ArgumentParser

from lib import Hyprctl

class SwapWorkspace:
    def __init__(self, args, hypr: Hyprctl):
        self.args = args
        self._hypr = hypr

    def run(self):
        current = self._active_workspace_id()
        target = self.args.to if self.args.to else self._neighbor(self.args.swap)
        self._swap(current, target)

    def _active_workspace_id(self) -> int:
        ws = self._hypr.active_workspace()
        if not ws:
            print("no active workspace", file=sys.stderr)
            sys.exit(1)

        return ws["id"]

    def _active_monitor_id(self) -> int:
        focused = self._hypr.focused_monitor()
        if focused:
            return focused["id"]
        monitors = self._hypr.monitors()
        if not monitors:
            print("no monitors found", file=sys.stderr)
            sys.exit(1)

        return monitors[0]["id"]

    def _monitor_workspace_ids(self, monitor_id: int) -> list[int]:
        return sorted(
            ws["id"]
            for ws in self._hypr.workspaces()
            if ws["monitorID"] == monitor_id and ws["id"] > 0
        )

    def _windows_on(self, workspace_id: int) -> list[str]:
        return [
            c["address"]
            for c in self._hypr.clients()
            if c["workspace"]["id"] == workspace_id
        ]

    def _neighbor(self, direction: str) -> int:
        current = self._active_workspace_id()
        ws_ids = self._monitor_workspace_ids(self._active_monitor_id())
        if not ws_ids or current not in ws_ids:
            return current

        idx = ws_ids.index(current)
        if direction == "left":
            return ws_ids[idx - 1] if idx > 0 else ws_ids[-1]

        return ws_ids[idx + 1] if idx < len(ws_ids) - 1 else ws_ids[0]

    def _swap(self, current: int, target: int) -> None:
        if current == target:
            return

        current_windows = self._windows_on(current)
        target_windows = self._windows_on(target)

        if not current_windows and not target_windows:
            self._hypr.dispatch("workspace", str(target))
            return

        # Track by address, so moving current→target first and then target→current
        # is safe — the original target windows carry their address through the
        # swap.
        for addr in current_windows:
            self._hypr.dispatch("movetoworkspacesilent", f"{target},address:{addr}")
        for addr in target_windows:
            self._hypr.dispatch("movetoworkspacesilent", f"{current},address:{addr}")

        self._hypr.dispatch("workspace", str(target))

def main():
    parser = ArgumentParser(description="Swap workspace positions in Hyprland")
    parser.add_argument(
        "-t", "--to",
        type=int,
        help="Move current workspace to the given position",
    )
    parser.add_argument(
        "-s", "--swap",
        choices=["left", "right"],
        help="Swap with the left/right workspace on the current monitor",
    )
    args = parser.parse_args()

    if not args.to and not args.swap:
        parser.error("Either -t/--to or -s/--swap must be specified")

    SwapWorkspace(args, Hyprctl()).run()

if __name__ == "__main__":
    main()
