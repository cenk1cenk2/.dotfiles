#!/usr/bin/env python3
"""Swap workspace positions in Sway by renaming workspaces.

- `-t/--to N`: move current workspace to N (swaps with existing).
- `-s/--swap left|right`: swap with the previous/next workspace on the
  current monitor.
"""

from argparse import ArgumentParser

from lib import Swayctl

class SwapWorkspace:
    def __init__(self, args, sway: Swayctl):
        self.args = args
        self._sway = sway

    def run(self):
        current = self._sway.active_workspace()
        numbers = set(self._sway.workspace_numbers())
        temp = max(numbers) + 1

        target = self._resolve_target(current.num)
        if target is None:
            return

        if target == current.num:
            print("Already at boundary, no swap needed")
            return

        self._swap(current.num, target, numbers, temp)

    def _resolve_target(self, current_num: int) -> int | None:
        if self.args.to:
            return self.args.to
        if self.args.swap:
            return self._neighbor(self.args.swap, current_num)

        return None

    def _neighbor(self, direction: str, current_num: int) -> int:
        active = self._sway.active_workspace()
        on_monitor = self._sway.workspaces_on_output(active.ipc_data["output"])
        nums = sorted(ws.num for ws in on_monitor if ws.num > 0)

        if direction == "left":
            candidates = [n for n in nums if n < current_num]

            return max(candidates) if candidates else (max(nums) if nums else current_num)
        if direction == "right":
            candidates = [n for n in nums if n > current_num]

            return min(candidates) if candidates else (min(nums) if nums else current_num)

        raise ValueError(f"Invalid swap direction: {direction}")

    def _swap(
        self,
        current_num: int,
        target: int,
        numbers: set[int],
        temp: int,
    ) -> None:
        # Use a temp number as staging so the rename of `current -> target`
        # doesn't collide with an existing workspace of that number.
        if target in numbers and target != current_num:
            self._sway.command(f"rename workspace number {target} to {temp}")

        self._sway.command(
            f"rename workspace number {current_num} to {target}, "
            f"workspace number {target}"
        )

        if target in numbers and target != current_num:
            self._sway.command(f"rename workspace number {temp} to {current_num}")

def main():
    parser = ArgumentParser(description="Swap workspace positions in Sway")
    parser.add_argument(
        "-t", "--to",
        type=int,
        help="Move current workspace to the given position.",
    )
    parser.add_argument(
        "-s", "--swap",
        choices=["left", "right"],
        help="Swap with the left/right workspace on the current monitor.",
    )
    args = parser.parse_args()
    assert args.to or args.swap, "either -t/--to or -s/--swap is required"

    SwapWorkspace(args, Swayctl()).run()

if __name__ == "__main__":
    main()
