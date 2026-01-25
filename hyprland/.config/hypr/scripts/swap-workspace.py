#!/usr/bin/env python3

"""
Swap workspace positions in Hyprland by moving all windows between workspaces.

This script allows you to:
- Move current workspace to a specific number (-t/--to): swaps with existing
- Swap left (-s left): moves windows to previous workspace on current monitor
- Swap right (-s right): moves windows to next workspace on current monitor

For -s left/right, uses Hyprland's m+1/m-1 to find the next workspace on the
current monitor, keeping workspaces monitor-local.
"""

import json
import subprocess
from argparse import ArgumentParser
from typing import List, Dict, Any

def hyprctl_json(command: str) -> Any:
    """Execute hyprctl command and return JSON output."""
    result = subprocess.run(
        ["hyprctl", "-j"] + command.split(),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)

def hyprctl_batch(commands: List[str]) -> None:
    """Execute multiple hyprctl dispatch commands in a batch."""
    if not commands:
        return

    batch_cmd = " ; ".join(commands)

    result = subprocess.run(
        ["hyprctl", "--batch", batch_cmd],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print(f"Batch command failed: {result.stderr}")

def get_windows_on_workspace(workspace_id: int) -> List[str]:
    """Get all window addresses on a specific workspace."""
    clients = hyprctl_json("clients")
    return [
        client["address"]
        for client in clients
        if client["workspace"]["id"] == workspace_id
    ]

def get_workspace_numbers() -> List[int]:
    """Get list of existing workspace numbers."""
    workspaces = hyprctl_json("workspaces")
    return [ws["id"] for ws in workspaces if ws["id"] > 0]

def get_active_workspace() -> Dict[str, Any]:
    """Get current active workspace info."""
    return hyprctl_json("activeworkspace")

def get_workspace_after_relative_move(direction: str) -> int:
    """Get the workspace ID after a relative move (m+1 or m-1)."""
    current_ws = get_active_workspace()
    current_id = current_ws["id"]

    # Execute the relative move
    subprocess.run(
        ["hyprctl", "dispatch", "workspace", direction],
        capture_output=True,
        check=True,
    )

    # Get the new workspace ID
    new_ws = get_active_workspace()
    target_id = new_ws["id"]

    # Move back to original workspace
    subprocess.run(
        ["hyprctl", "dispatch", "workspace", str(current_id)],
        capture_output=True,
        check=True,
    )

    return target_id

def move_all_windows(from_ws: int, to_ws: int) -> None:
    """Move all windows from one workspace to another."""
    windows = get_windows_on_workspace(from_ws)

    if not windows:
        return

    commands = []
    for addr in windows:
        commands.append(f"dispatch movetoworkspacesilent {to_ws},address:{addr}")

    hyprctl_batch(commands)

def swap_workspaces(current: int, target: int) -> None:
    """Swap all windows between two workspaces."""
    workspace_numbers = get_workspace_numbers()

    # Get windows on both workspaces
    current_windows = get_windows_on_workspace(current)
    target_windows = get_windows_on_workspace(target)

    if not current_windows and not target_windows:
        # Nothing to swap
        subprocess.run(["hyprctl", "dispatch", "workspace", str(target)], check=False)
        return

    # Use a temporary workspace that doesn't exist
    temp_ws = max(workspace_numbers + [current, target]) + 1

    commands = []

    # Move target workspace windows to temp
    for addr in target_windows:
        commands.append(f"dispatch movetoworkspacesilent {temp_ws},address:{addr}")

    # Move current workspace windows to target
    for addr in current_windows:
        commands.append(f"dispatch movetoworkspacesilent {target},address:{addr}")

    # Move temp windows to current
    for addr in target_windows:
        commands.append(f"dispatch movetoworkspacesilent {current},address:{addr}")

    # Switch to the target workspace
    commands.append(f"dispatch workspace {target}")

    hyprctl_batch(commands)

def main():
    parser = ArgumentParser(description="Swap workspace positions in Hyprland")
    parser.add_argument(
        "-t",
        "--to",
        type=int,
        help="Move current workspace to the given position",
    )
    parser.add_argument(
        "-s",
        "--swap",
        choices=["left", "right"],
        help="Swap workspace with the one to the left or right on current monitor",
    )

    args = parser.parse_args()

    if not args.to and not args.swap:
        parser.error("Either -t/--to or -s/--swap must be specified")

    # Get current state
    active_ws = get_active_workspace()
    current_workspace = active_ws["id"]

    if args.to:
        target = args.to
        swap_workspaces(current_workspace, target)

    elif args.swap:
        # Use Hyprland's m+1/m-1 to find the next workspace on current monitor
        if args.swap == "left":
            target = get_workspace_after_relative_move("m-1")
        else:  # right
            target = get_workspace_after_relative_move("m+1")

        # Swap with the target workspace
        swap_workspaces(current_workspace, target)

if __name__ == "__main__":
    main()
