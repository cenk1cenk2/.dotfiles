#!/usr/bin/env python3
"""Toggle focus between floating and tiled windows."""

import sys

from lib import Hyprctl

class FloatFocusToggler:
    def __init__(self, args, hypr: Hyprctl):
        self.args = args
        self._hypr = hypr

    def run(self):
        active = self._hypr.active_window()
        if not active:
            self._hypr.dispatch("cyclenext")
            return

        target = "tiled" if active.get("floating", False) else "floating"
        if not self._hypr.dispatch("cyclenext", target):
            print(f"Error: failed to cycle to {target} window", file=sys.stderr)
            sys.exit(1)

def main():
    FloatFocusToggler(args=None, hypr=Hyprctl()).run()

if __name__ == "__main__":
    main()
