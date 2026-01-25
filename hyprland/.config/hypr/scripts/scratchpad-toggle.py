#!/usr/bin/env python3
"""Toggle active window to/from scratchpad."""

import json
import subprocess
import sys

def run_hyprctl(args):
    """Run hyprctl command and return JSON output."""
    result = subprocess.run(
        ["hyprctl", *args, "-j"], capture_output=True, text=True, check=True
    )
    return json.loads(result.stdout)

def get_current_workspace():
    """Get the current visible workspace ID."""
    monitors = run_hyprctl(["monitors"])
    for monitor in monitors:
        if monitor.get("focused"):
            return monitor.get("activeWorkspace", {}).get("id")
    return 1  # fallback

def get_scratchpad_windows():
    """Get list of window addresses in scratchpad."""
    clients = run_hyprctl(["clients"])
    return {
        client["address"]
        for client in clients
        if client.get("workspace", {}).get("name", "") == "special:scratch"
    }

def main():
    try:
        # Get active window info
        active_window = run_hyprctl(["activewindow"])

        if not active_window or active_window.get("address") == "":
            print("No active window")
            sys.exit(1)

        window_address = active_window.get("address")
        current_workspace = active_window.get("workspace", {}).get("name", "")
        scratchpad_windows = get_scratchpad_windows()

        if current_workspace == "special:scratch":
            # Window is currently in scratchpad, move it to current workspace
            visible_workspace_id = get_current_workspace()
            subprocess.run(
                ["hyprctl", "dispatch", "movetoworkspace", str(visible_workspace_id)],
                check=True,
            )
        elif window_address in scratchpad_windows:
            # Window was in scratchpad but is now visible, move it back
            subprocess.run(
                ["hyprctl", "dispatch", "movetoworkspace", "special:scratch"],
                check=True,
            )
        else:
            # Window is on a regular workspace, move it to scratchpad
            subprocess.run(
                ["hyprctl", "dispatch", "movetoworkspace", "special:scratch"],
                check=True,
            )

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
