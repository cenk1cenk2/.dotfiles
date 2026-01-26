#!/usr/bin/env python3
"""Toggle focus between floating and tiled windows."""

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
        # Get active window
        active_window = run_hyprctl(["activewindow"])

        if not active_window or active_window.get("address") == "":
            # No active window, try to focus any window
            subprocess.run(["hyprctl", "dispatch", "cyclenext"], check=True)
            sys.exit(0)

        is_floating = active_window.get("floating", False)

        if is_floating:
            # Currently on floating, focus a tiled window
            subprocess.run(["hyprctl", "dispatch", "cyclenext", "tiled"], check=True)
        else:
            # Currently on tiled, focus a floating window
            subprocess.run(["hyprctl", "dispatch", "cyclenext", "floating"], check=True)

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
