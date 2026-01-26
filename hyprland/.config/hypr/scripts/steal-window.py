#!/usr/bin/env python3

"""
Steal a window from another workspace and bring it to the current workspace.

This script shows a rofi menu with all windows from other workspaces,
allowing you to select one to move to your current workspace.
Similar to swayr's steal-window command.
"""

import json
import subprocess
import sys
from typing import List, Dict, Any
from pathlib import Path

# Import icon lookup utility
import importlib.util

spec = importlib.util.spec_from_file_location(
    "window_icons", Path(__file__).parent / "window_icons.py"
)
window_icons = importlib.util.module_from_spec(spec)
spec.loader.exec_module(window_icons)

def hyprctl_json(command: str) -> Any:
    """Execute hyprctl command and return JSON output."""
    result = subprocess.run(
        ["hyprctl", "-j"] + command.split(),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)

def get_active_workspace() -> int:
    """Get current active workspace ID."""
    active = hyprctl_json("activeworkspace")
    return active["id"]

def get_all_windows() -> List[Dict[str, Any]]:
    """Get all windows from all workspaces."""
    return hyprctl_json("clients")

def format_window_entry(window: Dict[str, Any]) -> str:
    """Format a window entry for rofi display."""
    title = window.get("title", "Untitled")
    class_name = window.get("class", "Unknown")
    workspace = window.get("workspace", {}).get("id", "?")

    return f"[{workspace}] {title} - {class_name}"

def get_icon_name(class_name: str) -> str:
    """Get icon name for an application class using dynamic lookup."""
    return window_icons.get_icon_for_class(class_name)

def show_rofi_menu(entries: List[str], icons: List[str]) -> int:
    """Show rofi menu with icons and return selected index, or -1 if cancelled."""
    if not entries:
        return -1

    # Create rofi input with icon metadata using the rofi meta format
    rofi_input = ""
    for entry, icon in zip(entries, icons):
        rofi_input += f"{entry}\x00icon\x1f{icon}\n"

    try:
        result = subprocess.run(
            [
                "rofi",
                "-dmenu",
                "-i",  # case insensitive
                "-p",
                "Steal window",
                "-format",
                "i",  # return index
                "-show-icons",  # show icons
                "-theme-str",
                "window { width: 60%; }",
                "-theme-str",
                "listview { lines: 15; }",
            ],
            input=rofi_input,
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            # User cancelled
            return -1

        return int(result.stdout.strip())

    except (ValueError, subprocess.CalledProcessError):
        return -1

def steal_window(window_address: str, target_workspace: int) -> None:
    """Move a window to the target workspace."""
    subprocess.run(
        [
            "hyprctl",
            "dispatch",
            f"movetoworkspace",
            f"{target_workspace},address:{window_address}",
        ],
        check=True,
    )

def main():
    # Get current workspace
    current_workspace = get_active_workspace()

    # Get all windows
    all_windows = get_all_windows()

    # Filter windows from other workspaces
    other_windows = [
        w for w in all_windows if w.get("workspace", {}).get("id") != current_workspace
    ]

    if not other_windows:
        print("No windows available to steal")
        sys.exit(0)

    # Sort by workspace ID for better organization
    other_windows.sort(key=lambda w: w.get("workspace", {}).get("id", 0))

    # Create rofi entries and icons
    entries = [format_window_entry(w) for w in other_windows]
    icons = [get_icon_name(w.get("class", "Unknown")) for w in other_windows]

    # Show rofi menu
    selected_index = show_rofi_menu(entries, icons)

    if selected_index < 0 or selected_index >= len(other_windows):
        # User cancelled or invalid selection
        sys.exit(0)

    # Get selected window
    selected_window = other_windows[selected_index]
    window_address = selected_window["address"]

    # Steal the window
    steal_window(window_address, current_workspace)

    print(f"Stole window: {selected_window.get('title', 'Untitled')}")

if __name__ == "__main__":
    main()
