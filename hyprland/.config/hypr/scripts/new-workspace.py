#!/usr/bin/python3

from argparse import ArgumentParser
import json
import subprocess

def run_hyprctl(command):
    """Run a hyprctl command and return the output"""
    result = subprocess.run(
        ["hyprctl", "-j"] + command.split(),
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)

def run_hyprctl_dispatch(command):
    """Run a hyprctl dispatch command"""
    result = subprocess.run(
        ["hyprctl", "dispatch"] + command.split(),
        capture_output=True,
        text=True,
    )
    return result.returncode == 0

if __name__ == "__main__":
    arguments_parser = ArgumentParser()
    arguments_parser.add_argument(
        "-s",
        "--switch",
        action="store_true",
        help="switch to the first empty workspace",
    )
    arguments_parser.add_argument(
        "-m",
        "--move",
        action="store_true",
        help="move the currently focused container to the first empty workspace",
    )
    arguments = arguments_parser.parse_args()
    assert arguments.switch or arguments.move, (
        "at least one of --switch or --move must be specified"
    )

    # Get all workspaces (across all monitors, same as Sway script)
    workspaces = run_hyprctl("workspaces")
    workspace_numbers = [workspace["id"] for workspace in workspaces]

    # Get the lowest empty workspace number (same logic as Sway script)
    target = min(set(range(1, max(workspace_numbers) + 2)) - set(workspace_numbers))

    # Execute the requested action
    if arguments.move and arguments.switch:
        # Move container and switch in one command
        run_hyprctl_dispatch(f"movetoworkspace {target}")
        run_hyprctl_dispatch(f"workspace {target}")
    elif arguments.switch:
        run_hyprctl_dispatch(f"workspace {target}")
    elif arguments.move:
        run_hyprctl_dispatch(f"movetoworkspace {target}")
