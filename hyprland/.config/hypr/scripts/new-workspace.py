#!/usr/bin/env python3
"""Switch to or move a window to the first empty workspace."""

from argparse import ArgumentParser

from lib import Hyprctl

class NewWorkspace:
    def __init__(self, args, hypr: Hyprctl):
        self.args = args
        self._hypr = hypr

    def run(self):
        target = self._first_empty_workspace()
        if self.args.move:
            self._hypr.dispatch("movetoworkspace", target)
        if self.args.switch:
            self._hypr.dispatch("workspace", target)

    def _first_empty_workspace(self) -> str:
        used = {ws["id"] for ws in self._hypr.workspaces()}

        return str(min(set(range(1, max(used, default=0) + 2)) - used))

def main():
    parser = ArgumentParser()
    parser.add_argument(
        "-s", "--switch",
        action="store_true",
        help="switch to the first empty workspace",
    )
    parser.add_argument(
        "-m", "--move",
        action="store_true",
        help="move the currently focused container to the first empty workspace",
    )
    args = parser.parse_args()
    assert args.switch or args.move, (
        "at least one of --switch or --move must be specified"
    )

    NewWorkspace(args, Hyprctl()).run()

if __name__ == "__main__":
    main()
