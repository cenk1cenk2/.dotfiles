#!/usr/bin/env python3

"""
Utility module for dynamically looking up application icons.

This module searches desktop files to find the correct icon for a given
window class, providing better icon coverage than static mappings.
"""

import os
import re
from pathlib import Path
from typing import Optional, Dict
from functools import lru_cache

# Cache for desktop file lookups
_desktop_file_cache: Dict[str, str] = {}
_desktop_files_scanned = False

def _scan_desktop_files() -> None:
    """Scan all desktop files and build a cache of class -> icon mappings."""
    global _desktop_file_cache, _desktop_files_scanned

    if _desktop_files_scanned:
        return

    # Desktop file search paths
    search_paths = [
        Path.home() / ".local/share/applications",
        Path("/usr/share/applications"),
        Path("/usr/local/share/applications"),
        Path("/var/lib/flatpak/exports/share/applications"),
        Path.home() / ".local/share/flatpak/exports/share/applications",
    ]

    for search_path in search_paths:
        if not search_path.exists():
            continue

        for desktop_file in search_path.glob("*.desktop"):
            try:
                with open(desktop_file, "r", encoding="utf-8") as f:
                    content = f.read()

                    # Extract Icon, StartupWMClass, and Name
                    icon_match = re.search(r"^Icon=(.+)$", content, re.MULTILINE)
                    wm_class_match = re.search(
                        r"^StartupWMClass=(.+)$", content, re.MULTILINE
                    )
                    name_match = re.search(r"^Name=(.+)$", content, re.MULTILINE)

                    icon = icon_match.group(1).strip() if icon_match else None

                    if not icon:
                        continue

                    # Try to map by WM class first (most accurate)
                    if wm_class_match:
                        wm_class = wm_class_match.group(1).strip()
                        _desktop_file_cache[wm_class] = icon
                        _desktop_file_cache[wm_class.lower()] = icon

                    # Also map by desktop file basename (common pattern)
                    basename = desktop_file.stem
                    _desktop_file_cache[basename] = icon
                    _desktop_file_cache[basename.lower()] = icon

                    # Map by application name
                    if name_match:
                        name = name_match.group(1).strip()
                        _desktop_file_cache[name] = icon
                        _desktop_file_cache[name.lower()] = icon

            except Exception:
                # Skip files that can't be read
                continue

    _desktop_files_scanned = True

@lru_cache(maxsize=128)
def get_icon_for_class(window_class: str) -> str:
    """
    Get the icon name for a window class.

    This function:
    1. Scans desktop files to find icon mappings
    2. Tries exact match, lowercase match, and common patterns
    3. Falls back to the window class itself

    Args:
        window_class: The window class from Hyprland

    Returns:
        Icon name suitable for rofi's icon system
    """
    if not window_class:
        return "application-x-executable"

    # Ensure desktop files are scanned
    _scan_desktop_files()

    # Try exact match
    if window_class in _desktop_file_cache:
        return _desktop_file_cache[window_class]

    # Try lowercase
    if window_class.lower() in _desktop_file_cache:
        return _desktop_file_cache[window_class.lower()]

    # Try common transformations
    # Handle org.something.App -> org.something.App and app
    if "." in window_class:
        parts = window_class.split(".")
        # Try the last part (app name)
        if parts[-1].lower() in _desktop_file_cache:
            return _desktop_file_cache[parts[-1].lower()]

    # Try removing common suffixes
    for suffix in ["-browser", ".desktop", "-bin"]:
        if window_class.lower().endswith(suffix):
            base = window_class[: -len(suffix)]
            if base.lower() in _desktop_file_cache:
                return _desktop_file_cache[base.lower()]

    # Manual fallbacks for common apps that don't match well
    manual_map = {
        "brave-browser": "brave-browser",
        "Brave-browser": "brave-browser",
        "google-chrome": "google-chrome",
        "firefox": "firefox",
        "kitty": "kitty",
        "Alacritty": "Alacritty",
        "Code": "visual-studio-code",
        "code": "visual-studio-code",
    }

    if window_class in manual_map:
        return manual_map[window_class]

    # Last resort: return the class itself (GTK will try to find it)
    return window_class.lower()

if __name__ == "__main__":
    # Test the icon lookup
    import sys

    if len(sys.argv) > 1:
        test_class = sys.argv[1]
        icon = get_icon_for_class(test_class)
        print(f"{test_class} -> {icon}")
    else:
        # Test some common apps
        test_classes = [
            "Brave-browser",
            "kitty",
            "Slack",
            "spotify",
            "org.gnome.Nautilus",
            "code",
        ]

        for cls in test_classes:
            icon = get_icon_for_class(cls)
            print(f"{cls:30} -> {icon}")
