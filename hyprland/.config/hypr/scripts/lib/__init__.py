"""Shared building blocks for Hyprland control scripts.

Typical usage:

    from dotlib.hyprctl import Hyprctl
    from lib import rofi, rofi_with_icons
    from lib.window_icons import get_icon_for_class

    hypr = Hyprctl()
    win = hypr.active_window()
    hypr.dispatch('hl.dsp.focus({ workspace = "3" })')
"""

from .rofi import rofi as rofi
from .rofi import rofi_with_icons as rofi_with_icons
from .window_icons import get_icon_for_class as get_icon_for_class
