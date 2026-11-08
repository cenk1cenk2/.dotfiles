"""Shared building blocks for Hyprland control scripts.

Typical usage:

    from lib import Hyprctl, rofi
    from lib.window_icons import get_icon_for_class

    hypr = Hyprctl()
    win = hypr.active_window()
    hypr.dispatch('hl.dsp.focus({ workspace = "3" })')
"""

from .hyprctl import Hyprctl as Hyprctl
from .rofi import rofi as rofi
from .window_icons import get_icon_for_class as get_icon_for_class
from .window_icons import get_name_for_class as get_name_for_class
