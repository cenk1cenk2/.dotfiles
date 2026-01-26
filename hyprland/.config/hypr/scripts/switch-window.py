#!/usr/bin/env python3

"""
Switch to any window from any workspace.

This script shows a rofi menu with all windows from all workspaces,
allowing you to select one to focus. Similar to swayr's switch-window command.
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

def get_focused_window() -> str:
    """Get the address of the currently focused window."""
    active = hyprctl_json("activewindow")
    return active.get("address", "")

def get_all_windows() -> List[Dict[str, Any]]:
    """Get all windows from all workspaces."""
    return hyprctl_json("clients")

def format_window_entry(window: Dict[str, Any], is_current: bool = False) -> str:
    """Format a window entry for rofi display."""
    title = window.get("title", "Untitled")
    class_name = window.get("class", "Unknown")
    workspace = window.get("workspace", {}).get("id", "?")

    # Truncate title if too long
    if len(title) > 60:
        title = title[:57] + "..."

    # Mark the currently focused window
    marker = "● " if is_current else "  "

    return f"{marker}[WS {workspace}] {title} - {class_name}"

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
                "Switch window",
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

def focus_window(window_address: str) -> None:
    """Focus a window by its address."""
    subprocess.run(
        [
            "hyprctl",
            "dispatch",
            "focuswindow",
            f"address:{window_address}",
        ],
        check=True,
    )

def main():
    # Get all windows
    all_windows = get_all_windows()

    if not all_windows:
        print("No windows available")
        sys.exit(0)

    # Get currently focused window
    focused_address = get_focused_window()

    # Sort windows: current workspace first, then by workspace ID
    current_workspace = get_active_workspace()

    def sort_key(w):
        ws_id = w.get("workspace", {}).get("id", 999)
        is_current_ws = 0 if ws_id == current_workspace else 1
        is_focused = 0 if w.get("address") == focused_address else 1
        return (is_current_ws, is_focused, ws_id)

    all_windows.sort(key=sort_key)

    # Create rofi entries and icons
    entries = [
        format_window_entry(w, w.get("address") == focused_address) for w in all_windows
    ]
    icons = [get_icon_name(w.get("class", "Unknown")) for w in all_windows]

    # Show rofi menu
    selected_index = show_rofi_menu(entries, icons)

    if selected_index < 0 or selected_index >= len(all_windows):
        # User cancelled or invalid selection
        sys.exit(0)

    # Get selected window
    selected_window = all_windows[selected_index]
    window_address = selected_window["address"]

    # Focus the window
    focus_window(window_address)

    print(f"Switched to window: {selected_window.get('title', 'Untitled')}")

if __name__ == "__main__":
    main()
