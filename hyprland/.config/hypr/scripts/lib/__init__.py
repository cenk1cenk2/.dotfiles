"""Shared building blocks for Hyprland control scripts.

Typical usage:

    from lib import Hyprctl, notify, rofi, rofi_with_icons
    from lib.window_icons import get_icon_for_class

    hypr = Hyprctl()
    win = hypr.active_window()
    hypr.dispatch('hl.dsp.focus({ workspace = "3" })')
"""

from .cli import create_logger as create_logger
from .hyprctl import Hyprctl as Hyprctl
from .notify import notify as notify
from .rofi import rofi as rofi, rofi_with_icons as rofi_with_icons
from .window_icons import get_icon_for_class as get_icon_for_class
