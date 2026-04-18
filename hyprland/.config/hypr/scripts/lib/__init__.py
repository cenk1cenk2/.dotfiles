"""Shared building blocks for Hyprland control scripts.

Typical usage:

    from lib import Hyprctl, notify, rofi, rofi_with_icons
    from lib.window_icons import get_icon_for_class

    hypr = Hyprctl()
    win = hypr.active_window()
    hypr.dispatch("workspace", "3")
"""

from .hyprctl import Hyprctl
from .notify import notify
from .rofi import rofi, rofi_with_icons
from .window_icons import get_icon_for_class

__all__ = [
    "Hyprctl",
    "notify",
    "rofi",
    "rofi_with_icons",
    "get_icon_for_class",
]
