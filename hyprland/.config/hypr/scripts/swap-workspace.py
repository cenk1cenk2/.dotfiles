#!/usr/bin/env python3

"""
Swap workspace positions in Hyprland by moving all windows between workspaces.

This script allows you to:
- Move current workspace to a specific number (-t/--to): swaps with existing
- Swap left (-s left): swaps with previous workspace on current monitor
- Swap right (-s right): swaps with next workspace on current monitor
"""

import json
import subprocess
import sys
from argparse import ArgumentParser
from typing import List, Any

def hyprctl_json(command: str) -> Any:
    result = subprocess.run(
        ["hyprctl", "-j"] + command.split(),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"hyprctl {command} failed: {result.stderr}", file=sys.stderr)
        sys.exit(1)

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"Failed to parse hyprctl {command} output", file=sys.stderr)
        sys.exit(1)

def hyprctl_dispatch(command: str, args: str) -> None:
    result = subprocess.run(
        ["hyprctl", "dispatch", command, args],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print(f"dispatch {command} {args} failed: {result.stderr}", file=sys.stderr)

def get_active_workspace_id() -> int:
    ws = hyprctl_json("activeworkspace")

    return ws["id"]

def get_active_monitor_id() -> int:
    monitors = hyprctl_json("monitors")
    for mon in monitors:
        if mon["focused"]:
            return mon["id"]

    return monitors[0]["id"]

def get_monitor_workspace_ids(monitor_id: int) -> List[int]:
    workspaces = hyprctl_json("workspaces")
    ids = sorted(
        ws["id"] for ws in workspaces if ws["monitorID"] == monitor_id and ws["id"] > 0
    )

    return ids

def get_windows_on_workspace(workspace_id: int) -> List[str]:
    clients = hyprctl_json("clients")

    return [
        client["address"]
        for client in clients
        if client["workspace"]["id"] == workspace_id
    ]

def get_neighbor_workspace(direction: str) -> int:
    monitor_id = get_active_monitor_id()
    current_id = get_active_workspace_id()
    ws_ids = get_monitor_workspace_ids(monitor_id)

    if not ws_ids or current_id not in ws_ids:
        return current_id

    idx = ws_ids.index(current_id)

    if direction == "left":
        return ws_ids[idx - 1] if idx > 0 else ws_ids[-1]
    else:
        return ws_ids[idx + 1] if idx < len(ws_ids) - 1 else ws_ids[0]

def swap_workspaces(current: int, target: int) -> None:
    if current == target:
        return

    current_windows = get_windows_on_workspace(current)
    target_windows = get_windows_on_workspace(target)

    if not current_windows and not target_windows:
        hyprctl_dispatch("workspace", str(target))

        return

    # No temp workspace needed — we track windows by address, so we can
    # move current→target first, then move original target windows back
    for addr in current_windows:
        hyprctl_dispatch("movetoworkspacesilent", f"{target},address:{addr}")

    for addr in target_windows:
        hyprctl_dispatch("movetoworkspacesilent", f"{current},address:{addr}")

    hyprctl_dispatch("workspace", str(target))

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

    current_workspace = get_active_workspace_id()

    if args.to:
        swap_workspaces(current_workspace, args.to)
    elif args.swap:
        target = get_neighbor_workspace(args.swap)
        swap_workspaces(current_workspace, target)

if __name__ == "__main__":
    main()
