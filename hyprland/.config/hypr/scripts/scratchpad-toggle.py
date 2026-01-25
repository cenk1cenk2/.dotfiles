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

def main():
    try:
        # Get active window info
        active_window = run_hyprctl(["activewindow"])

        if not active_window or active_window.get("address") == "":
            print("No active window")
            sys.exit(1)

        current_workspace = active_window.get("workspace", {}).get("name", "")

        if current_workspace == "special:scratch":
            # Window is in scratchpad, move it back to current workspace
            subprocess.run(["hyprctl", "dispatch", "movetoworkspacesilent", "e+0"])
        else:
            # Window is not in scratchpad, move it there
            subprocess.run(
                ["hyprctl", "dispatch", "movetoworkspacesilent", "special:scratch"]
            )

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
