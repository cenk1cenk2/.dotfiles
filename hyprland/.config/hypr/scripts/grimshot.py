#!/usr/bin/env python3

"""
Grimshot: a helper for screenshots within Hyprland
Requirements:
 - `grim`: screenshot utility for wayland
 - `slurp`: to select an area
 - `hyprctl`: to read properties of current window
 - `wl-copy`: clipboard utility
 - `jq`: json utility to parse hyprctl output
 - `notify-send`: to show notifications
Those are needed to be installed, if unsure, run `grimshot.py check`

See `man 1 grimshot` or `grimshot.py usage` for further details.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

class GrimshotError(Exception):
    """Custom exception for grimshot errors"""

    pass

class Grimshot:
    def __init__(self):
        self.notify_enabled = False
        self.cursor_enabled = False
        self.wayfreeze_process = None

        # Set up signal handlers for cleanup
        signal.signal(signal.SIGTERM, self._cleanup_handler)
        signal.signal(signal.SIGINT, self._cleanup_handler)
        signal.signal(signal.SIGHUP, self._cleanup_handler)
        signal.signal(signal.SIGQUIT, self._cleanup_handler)
        signal.signal(signal.SIGABRT, self._cleanup_handler)

    def _cleanup_handler(self, signum, frame):
        """Signal handler for cleanup"""
        self.cleanup()
        sys.exit(0)

    def cleanup(self):
        """Kill wayfreeze process"""
        try:
            subprocess.run(
                ["killall", "wayfreeze"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except subprocess.SubprocessError:
            pass

    def get_target_directory(self) -> str:
        """Get the target directory for screenshots"""
        config_home = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
        user_dirs_file = Path(config_home) / "user-dirs.dirs"

        screenshots_dir = os.environ.get("XDG_SCREENSHOTS_DIR")
        if screenshots_dir:
            return screenshots_dir

        if user_dirs_file.exists():
            try:
                with open(user_dirs_file, "r") as f:
                    for line in f:
                        if line.startswith("XDG_SCREENSHOTS_DIR="):
                            return line.split("=", 1)[1].strip().strip('"')
                        elif line.startswith("XDG_PICTURES_DIR="):
                            pictures_dir = line.split("=", 1)[1].strip().strip('"')
                            return pictures_dir
            except (IOError, OSError):
                pass

        return os.environ.get("XDG_PICTURES_DIR", os.path.expanduser("~"))

    def notify(self, title: str, message: str = "", urgent: bool = False):
        """Send a notification"""
        cmd = ["notify-send", "-t", "3000", "-a", "grimshot"]
        if urgent:
            cmd.extend(["-u", "critical"])
        cmd.extend([title, message])

        try:
            subprocess.run(cmd, check=True)
        except subprocess.SubprocessError:
            pass

    def notify_ok(self, message: str = "OK", title: str = "Screenshot"):
        """Send a success notification"""
        if self.notify_enabled:
            self.notify(title, message)

    def notify_error(
        self,
        message: str = "Error taking screenshot with grim",
        title: str = "Screenshot",
    ):
        """Send an error notification or print to stderr"""
        if self.notify_enabled:
            self.notify(title, message, urgent=True)
        else:
            print(message, file=sys.stderr)

    def die(self, message: str = "Bye"):
        """Print error and exit"""
        self.notify_error(f"Error: {message}")
        sys.exit(2)

    def check_command(self, command: str) -> str:
        """Check if a command is available"""
        try:
            subprocess.run(
                ["command", "-v", command],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=True,
            )
            return "OK"
        except subprocess.SubprocessError:
            return "NOT FOUND"

    def check_requirements(self):
        """Check if all required tools are installed"""
        print(
            "Checking if required tools are installed. If something is missing, "
            "install it to your system and make it available in PATH..."
        )

        commands = [
            "grim",
            "slurp",
            "hyprctl",
            "wl-copy",
            "jq",
            "notify-send",
            "wayfreeze",
        ]
        for cmd in commands:
            result = self.check_command(cmd)
            print(f"   {cmd}: {result}")

    def run_command(
        self, cmd: list, capture_output: bool = True, input_data: bytes = None
    ) -> subprocess.CompletedProcess:
        """Run a command and return the result"""
        try:
            return subprocess.run(
                cmd,
                capture_output=capture_output,
                text=True if input_data is None else False,
                input=input_data,
                check=True,
            )
        except subprocess.SubprocessError as e:
            raise GrimshotError(f"Command failed: {' '.join(cmd)}") from e

    def take_screenshot(
        self, file_path: str, geometry: str = "", output: str = ""
    ) -> bool:
        """Take a screenshot using grim"""
        cmd = ["grim"]

        if self.cursor_enabled:
            cmd.append("-c")

        if output:
            cmd.extend(["-o", output])
        elif geometry:
            cmd.extend(["-g", geometry])

        cmd.append(file_path)

        try:
            self.run_command(cmd, capture_output=False)
            return True
        except GrimshotError:
            self.die("Unable to invoke grim")
            return False

    def get_area_geometry(self) -> Optional[str]:
        """Get geometry from user selection using slurp"""
        try:
            result = self.run_command(["slurp", "-d"])
            geometry = result.stdout.strip()
            return geometry if geometry else None
        except GrimshotError:
            return None

    def get_active_window_info(self) -> Tuple[str, str]:
        """Get geometry and class of the currently active window"""
        try:
            result = self.run_command(["hyprctl", "activewindow", "-j"])
            window = json.loads(result.stdout)

            if not window or "address" not in window:
                raise GrimshotError("No active window found")

            # Get window position and size
            at = window["at"]
            size = window["size"]
            geometry = f"{at[0]},{at[1]} {size[0]}x{size[1]}"

            # Get window class or title
            window_class = window.get("class", window.get("title", "Unknown"))

            return geometry, window_class
        except (GrimshotError, json.JSONDecodeError, KeyError) as e:
            self.die(f"Unable to get active window info: {e}")

    def get_focused_output(self) -> str:
        """Get the name of the currently focused output"""
        try:
            result = self.run_command(["hyprctl", "monitors", "-j"])
            monitors = json.loads(result.stdout)

            for monitor in monitors:
                if monitor.get("focused"):
                    return monitor["name"]

            raise GrimshotError("No focused monitor found")
        except (GrimshotError, json.JSONDecodeError, KeyError) as e:
            self.die(f"Unable to get focused output: {e}")

    def get_window_geometry(self) -> Optional[str]:
        """Get geometry of a window selected by the user"""
        try:
            # Get all clients (windows)
            result = self.run_command(["hyprctl", "clients", "-j"])
            clients = json.loads(result.stdout)

            # Build list of window geometries
            windows = []
            for client in clients:
                if not client.get("mapped"):
                    continue

                at = client["at"]
                size = client["size"]
                geometry = f"{at[0]},{at[1]} {size[0]}x{size[1]}"
                windows.append(geometry)

            if not windows:
                return None

            window_list = "\n".join(windows)

            # Use slurp to select from the list
            result = self.run_command(["slurp"], input_data=window_list.encode())
            geometry = result.stdout.strip()
            return geometry if geometry else None

        except (GrimshotError, json.JSONDecodeError, KeyError):
            return None

    def copy_to_clipboard(self, data: bytes):
        """Copy data to clipboard using wl-copy"""
        try:
            self.run_command(
                ["wl-copy", "--type", "image/png"],
                capture_output=False,
                input_data=data,
            )
        except GrimshotError:
            self.die("Clipboard error")

    def process_subject(self, subject: str) -> Tuple[str, str, str]:
        """Process the subject and return geometry, output, and description"""
        geometry = ""
        output = ""
        what = ""

        if subject == "area":
            geometry = self.get_area_geometry()
            if not geometry:
                sys.exit(1)
            what = "Area"

        elif subject == "active":
            geometry, window_class = self.get_active_window_info()
            what = f"{window_class} window"

        elif subject == "screen":
            what = "Screen"

        elif subject == "output":
            output = self.get_focused_output()
            what = output

        elif subject == "window":
            geometry = self.get_window_geometry()
            if not geometry:
                sys.exit(1)
            what = "Window"

        else:
            self.die(f"Unknown subject to take a screen shot from: {subject}")

        return geometry, output, what

    def main(self):
        """Main function"""
        parser = argparse.ArgumentParser(
            description="Grimshot: a helper for screenshots within Hyprland",
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog="""
Commands:
  copy: Copy the screenshot data into the clipboard.
  save: Save the screenshot to a regular file or '-' to pipe to STDOUT.
  check: Verify if required tools are installed and exit.
  usage: Show this message and exit.

Targets:
  active: Currently active window.
  screen: All visible outputs.
  output: Currently active output.
  area: Manually select a region.
  window: Manually select a window.
            """,
        )

        parser.add_argument(
            "-n", "--notify", action="store_true", help="Show notifications"
        )
        parser.add_argument(
            "-c", "--cursor", action="store_true", help="Include cursor in screenshot"
        )
        parser.add_argument(
            "action",
            nargs="?",
            default="usage",
            choices=["copy", "save", "check", "usage"],
            help="Action to perform",
        )
        parser.add_argument(
            "subject",
            nargs="?",
            default="screen",
            choices=["active", "screen", "output", "area", "window"],
            help="Subject to screenshot",
        )
        parser.add_argument(
            "file", nargs="?", help="Output file path (default: auto-generated)"
        )

        args = parser.parse_args()

        self.notify_enabled = args.notify
        self.cursor_enabled = args.cursor

        # Handle usage display
        if args.action == "usage":
            parser.print_help()
            return

        # Start wayfreeze
        try:
            self.wayfreeze_process = subprocess.Popen(["wayfreeze"])
            time.sleep(0.1)
        except subprocess.SubprocessError:
            pass  # wayfreeze is optional

        try:
            if args.action == "check":
                self.check_requirements()
                return

            # Set default file path if not provided
            if not args.file:
                target_dir = self.get_target_directory()
                timestamp = datetime.now().isoformat()
                args.file = f"{target_dir}/{timestamp}.png"

            # Process the subject
            geometry, output, what = self.process_subject(args.subject)

            if args.action == "copy":
                # Take screenshot and copy to clipboard
                if args.file == "-":
                    temp_file = "/tmp/grimshot.png"
                else:
                    temp_file = args.file

                if self.take_screenshot(temp_file, geometry, output):
                    with open(temp_file, "rb") as f:
                        screenshot_data = f.read()
                    self.copy_to_clipboard(screenshot_data)

                    if temp_file != args.file:
                        os.unlink(temp_file)

                    self.notify_ok(f"{what} copied to buffer")

            else:  # save
                if self.take_screenshot(args.file, geometry, output):
                    title = f"Screenshot of {args.subject}"
                    message = os.path.basename(args.file)
                    self.notify_ok(message, title)
                    print(args.file)
                else:
                    self.notify_error("Error taking screenshot with grim")

        finally:
            self.cleanup()

if __name__ == "__main__":
    grimshot = Grimshot()
    grimshot.main()
