#!/usr/bin/env python3
"""Display profile manager for Hyprland using kanshi via kanshictl."""

import argparse
import subprocess
import sys
from pathlib import Path

from lib import notify

KANSHI_CONFIG = Path.home() / ".config" / "kanshi" / "config"
UDEV_SCRIPT = Path("/etc/udev/scripts/wayland-user.sh")
ICON = "/usr/share/icons/Adwaita/scalable/devices/video-display.svg"

class DisplayProfile:
    def __init__(self, args):
        self.args = args

    def run(self):
        action = self.args.action
        if action == "help":
            self.args.parser.print_help()
        elif action == "ls":
            self._list_profiles()
        elif action == "reload":
            self._reload()
        else:
            self._switch_profile(action)

    def _list_profiles(self):
        if not KANSHI_CONFIG.exists():
            print("No kanshi config found", file=sys.stderr)
            sys.exit(1)

        try:
            with open(KANSHI_CONFIG) as f:
                profiles: list[str] = []
                for raw in f:
                    line = raw.strip()
                    if not line.startswith("profile "):
                        continue
                    parts = line.split()
                    if len(parts) >= 2 and parts[1] not in profiles:
                        profiles.append(parts[1])
            for profile in profiles:
                print(profile)
        except IOError as e:
            print(f"Error reading kanshi config: {e}", file=sys.stderr)
            sys.exit(1)

    def _reload(self):
        try:
            self._run(["kanshictl", "reload"], capture_output=False)
        except subprocess.SubprocessError:
            print("Error reloading kanshi", file=sys.stderr)
            sys.exit(1)

    def _switch_profile(self, profile: str):
        # Prefer the udev shim when present so the switch runs in a predictable
        # environment; fall back to kanshictl directly.
        cmd = (
            [str(UDEV_SCRIPT), "kanshictl", "switch", profile]
            if UDEV_SCRIPT.exists()
            else ["kanshictl", "switch", profile]
        )
        try:
            self._run(cmd)
            notify("Display", f"Trigger profile {profile}.", icon=ICON)
        except subprocess.SubprocessError as e:
            print(f"Error switching to profile {profile}: {e}", file=sys.stderr)
            notify("Display", f"Failed to switch to profile {profile}.", icon=ICON)
            sys.exit(1)

    @staticmethod
    def _run(cmd: list[str], capture_output: bool = True):
        return subprocess.run(
            cmd, capture_output=capture_output, text=True, check=True,
        )

def main():
    parser = argparse.ArgumentParser(
        description="Display profile manager using kanshi",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Commands:\n"
            "  ls:        List available profiles\n"
            "  reload:    Reload kanshi configuration\n"
            "  [profile]: Switch to the specified profile\n"
        ),
    )
    parser.add_argument(
        "action",
        nargs="?",
        default="help",
        help="Action to perform: ls, reload, or profile name",
    )
    args = parser.parse_args()
    args.parser = parser

    DisplayProfile(args).run()

if __name__ == "__main__":
    main()
