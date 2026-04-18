#!/usr/bin/env python3
"""Toggle the active window to/from the scratchpad."""

import sys

from lib import Hyprctl

SCRATCHPAD = "special:scratch"

class ScratchpadToggler:
    def __init__(self, args, hypr: Hyprctl):
        self.args = args
        self._hypr = hypr

    def run(self):
        active = self._hypr.active_window()
        if not active:
            print("No active window", file=sys.stderr)
            sys.exit(1)

        current_workspace = active.get("workspace", {}).get("name", "")
        address = active.get("address")

        if current_workspace == SCRATCHPAD:
            # Pulled up from scratchpad → send to currently focused workspace.
            target = self._hypr.focused_workspace_id() or 1
            self._hypr.dispatch("movetoworkspace", str(target))
            return

        if address in self._scratchpad_addresses():
            # Previously-scratched window that drifted away → push back.
            self._hypr.dispatch("movetoworkspace", SCRATCHPAD)
            return

        # Regular window → send to scratchpad.
        self._hypr.dispatch("movetoworkspace", SCRATCHPAD)

    def _scratchpad_addresses(self) -> set[str]:
        return {
            c["address"]
            for c in self._hypr.clients()
            if c.get("workspace", {}).get("name", "") == SCRATCHPAD
        }

def main():
    ScratchpadToggler(args=None, hypr=Hyprctl()).run()

if __name__ == "__main__":
    main()
