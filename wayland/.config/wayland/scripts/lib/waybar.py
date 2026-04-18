"""Waybar signalling helper."""

import subprocess

def signal_waybar(module: str) -> None:
    """Poke waybar to re-render the named custom module."""
    subprocess.run(["waybar-signal.sh", module], check=False)
