#!/usr/bin/env python3
"""Switch to or move the focused container to the first empty workspace."""

from argparse import ArgumentParser

from lib import Swayctl

class NewWorkspace:
    def __init__(self, args, sway: Swayctl):
        self.args = args
        self._sway = sway

    def run(self):
        target = self._sway.first_empty_workspace_number()

        if self.args.move and self.args.switch:
            # Combine both into a single command so Sway doesn't flicker the
            # wallpaper between the two steps.
            self._sway.command(
                f"move container to workspace number {target}, "
                f"workspace number {target}"
            )
            return

        if self.args.switch:
            self._sway.command(f"workspace number {target}")
        elif self.args.move:
            self._sway.command(f"move container to workspace number {target}")

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

    NewWorkspace(args, Swayctl()).run()

if __name__ == "__main__":
    main()
