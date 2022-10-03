#!/bin/bash

# Use swaymsg if WAYLAND_DISPLAY is set
WM_MSG=${WAYLAND_DISPLAY+swaymsg}
WM_MSG=${WM_MSG:-i3-msg}

VG27W_MODE="2560x1440@164.999Hz"
MAIN_OUTPUT="DP-4"
BENQ_MODE="2560x1440@99.897Hz"
SIDE_OUTPUT="DP-3"
VG27A_MODE="2560x1440@143.995Hz"
SALON_OUTPUT="DP-5"

if [ "$1" == "solo" ]; then
	swaymsg output $MAIN_OUTPUT enable
	swaymsg output $MAIN_OUTPUT mode "${VG27W_MODE}" pos 0 0
	swaymsg output $SIDE_OUTPUT disable
	swaymsg output $SALON_OUTPUT disable
elif [ "$1" == "dual" ]; then
	swaymsg output $MAIN_OUTPUT enable
	swaymsg output $MAIN_OUTPUT mode "${VG27W_MODE}" pos 0 0
	swaymsg output $SIDE_OUTPUT enable
	swaymsg output $SIDE_OUTPUT mode "${BENQ_MODE}" pos 2560 0
	swaymsg output $SALON_OUTPUT disable
elif [ "$1" == "salon" ]; then
	swaymsg output $SALON_OUTPUT enable
	swaymsg output $SALON_OUTPUT mode "${VG27A_MODE}" pos 5120 0
	swaymsg output $MAIN_OUTPUT disable
	swaymsg output $SIDE_OUTPUT disable
else
	echo "imdat"
fi
