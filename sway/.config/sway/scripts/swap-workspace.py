#!/usr/bin/python3

"""
Swap workspace positions in Sway by renaming workspaces.

This script allows you to:
- Move current workspace to a specific number (-t/--to): swaps with existing
- Swap left (-s left): swaps with previous workspace on current monitor
- Swap right (-s right): swaps with next workspace on current monitor

For -s left/right, only considers workspaces on the current monitor.
"""

from argparse import ArgumentParser

import i3ipc

def get_workspace_after_relative_move(ipc, direction):
    """Get the workspace number after a relative move (prev/next on monitor)."""
    tree = ipc.get_tree()
    current_ws = tree.find_focused().workspace()
    current_output = current_ws.ipc_data["output"]
    current_num = current_ws.num

    # Get all workspaces on current monitor
    workspaces_on_monitor = [
        ws
        for ws in tree.workspaces()
        if ws.ipc_data["output"] == current_output and ws.num > 0
    ]
    workspace_nums = sorted([ws.num for ws in workspaces_on_monitor])

    if direction == "left":
        # Find previous workspace on same monitor
        candidates = [num for num in workspace_nums if num < current_num]
        if candidates:
            return max(candidates)
        else:
            # Wrap to highest workspace on monitor
            return max(workspace_nums) if workspace_nums else current_num
    else:  # right
        # Find next workspace on same monitor
        candidates = [num for num in workspace_nums if num > current_num]
        if candidates:
            return min(candidates)
        else:
            # Wrap to lowest workspace on monitor
            return min(workspace_nums) if workspace_nums else current_num

if __name__ == "__main__":
    arguments_parser = ArgumentParser()
    arguments_parser.add_argument(
        "-t",
        "--to",
        type=int,
        action="store",
        help="Move workspace to the given position.",
    )
    arguments_parser.add_argument(
        "-s",
        "--swap",
        action="store",
        help="Swap workspace positionally.",
    )
    arguments = arguments_parser.parse_args()
    assert arguments.to or arguments.swap

    ipc = i3ipc.Connection()
    tree = ipc.get_tree()
    workspace = tree.find_focused().workspace()
    workspaces = tree.workspaces()
    workspace_number = workspace.num
    workspace_numbers = [workspace.num for workspace in workspaces]
    temp_workspace = max(workspace_numbers) + 1

    def command(command, should_assert=True):
        print(f"send: {command}")
        replies = ipc.command(command)

        if should_assert:
            for reply in replies:
                if reply.error:
                    print(f"fail: {command} -> {reply.error}")

                assert reply.success

    def target_to_temp(target):
        if target in set(workspace_numbers) and target != workspace_number:
            command(f"rename workspace number {target} to {temp_workspace}")

    def temp_to_initial(target, initial):
        if target in set(workspace_numbers) and target != workspace_number:
            command(f"rename workspace number {temp_workspace} to {initial}")

    if arguments.to:
        target = arguments.to

        target_to_temp(target)
        command(
            f"rename workspace number {workspace_number} to {target}, workspace number {target}"
        )
        temp_to_initial(target, workspace_number)

    elif arguments.swap:
        if arguments.swap == "left":
            target = get_workspace_after_relative_move(ipc, "left")
        elif arguments.swap == "right":
            target = get_workspace_after_relative_move(ipc, "right")
        else:
            raise Exception("Invalid swap argument.")

        if target == workspace_number:
            print("Already at boundary, no swap needed")
        else:
            target_to_temp(target)
            command(
                f"rename workspace number {workspace_number} to {target}, workspace number {target}"
            )
            temp_to_initial(target, workspace_number)
