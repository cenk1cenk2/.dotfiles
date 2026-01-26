#!/usr/bin/env python3

"""
Display profile manager for Hyprland using kanshi
Manages monitor configurations via kanshictl
"""

import argparse
import subprocess
import sys
from pathlib import Path

class DisplayProfileError(Exception):
    """Custom exception for display profile errors"""

    pass

class DisplayProfile:
    def __init__(self):
        self.kanshi_config = Path.home() / ".config" / "kanshi" / "config"
        self.udev_script = Path("/etc/udev/scripts/wayland-user.sh")

    def notify(self, title: str, message: str, icon: str = None):
        """Send a notification"""
        cmd = ["notify-send", title, message]
        if icon:
            cmd.extend(["-i", icon])

        try:
            subprocess.run(cmd, check=True)
        except subprocess.SubprocessError:
            pass

    def run_command(
        self, cmd: list, capture_output: bool = True
    ) -> subprocess.CompletedProcess:
        """Run a command and return the result"""
        try:
            return subprocess.run(
                cmd,
                capture_output=capture_output,
                text=True,
                check=True,
            )
        except subprocess.SubprocessError as e:
            raise DisplayProfileError(f"Command failed: {' '.join(cmd)}") from e

    def list_profiles(self):
        """List available kanshi profiles"""
        if not self.kanshi_config.exists():
            print("No kanshi config found", file=sys.stderr)
            sys.exit(1)

        try:
            with open(self.kanshi_config, "r") as f:
                profiles = []
                for line in f:
                    line = line.strip()
                    if line.startswith("profile "):
                        # Extract profile name (second word)
                        parts = line.split()
                        if len(parts) >= 2:
                            profile_name = parts[1]
                            if profile_name not in profiles:
                                profiles.append(profile_name)

                for profile in profiles:
                    print(profile)
        except IOError as e:
            print(f"Error reading kanshi config: {e}", file=sys.stderr)
            sys.exit(1)

    def reload(self):
        """Reload kanshi configuration"""
        try:
            self.run_command(["kanshictl", "reload"], capture_output=False)
        except DisplayProfileError:
            print("Error reloading kanshi", file=sys.stderr)
            sys.exit(1)

    def switch_profile(self, profile: str):
        """Switch to a specific kanshi profile"""
        try:
            # Use the udev script if it exists, otherwise use kanshictl directly
            if self.udev_script.exists():
                self.run_command(
                    [str(self.udev_script), "kanshictl", "switch", profile]
                )
            else:
                self.run_command(["kanshictl", "switch", profile])

            # Send notification on success
            self.notify(
                "Display",
                f"Trigger profile {profile}.",
                "/usr/share/icons/Adwaita/scalable/devices/video-display.svg",
            )
        except DisplayProfileError as e:
            print(f"Error switching to profile {profile}: {e}", file=sys.stderr)
            self.notify(
                "Display",
                f"Failed to switch to profile {profile}.",
                "/usr/share/icons/Adwaita/scalable/devices/video-display.svg",
            )
            sys.exit(1)

    def main(self):
        """Main function"""
        parser = argparse.ArgumentParser(
            description="Display profile manager using kanshi",
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog="""
Commands:
  ls: List available profiles
  reload: Reload kanshi configuration
  [profile]: Switch to the specified profile
            """,
        )

        parser.add_argument(
            "action",
            nargs="?",
            default="help",
            help="Action to perform: ls, reload, or profile name",
        )

        args = parser.parse_args()

        if args.action == "help":
            parser.print_help()
        elif args.action == "ls":
            self.list_profiles()
        elif args.action == "reload":
            self.reload()
        else:
            # Treat as profile name
            self.switch_profile(args.action)

if __name__ == "__main__":
    profile_manager = DisplayProfile()
    profile_manager.main()
