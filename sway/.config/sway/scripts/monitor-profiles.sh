#!/bin/bash

# Use swaymsg if WAYLAND_DISPLAY is set
WM_MSG=${WAYLAND_DISPLAY+swaymsg}
WM_MSG=${WM_MSG:-i3-msg}

VG27W_MODE="2560x1440@144.006Hz"
BENQ_MODE="2560x1440@143.856Hz"
VG27A_MODE="2560x1440@143.995Hz"

if [ "$1" == "solo" ]; then
	swaymsg output HDMI-A-1 enable
	swaymsg output HDMI-A-1 mode "${VG27W_MODE}" pos 0 0
	swaymsg output DP-3 disable
	swaymsg output DP-4 disable
elif [ "$1" == "dual" ]; then
	swaymsg output HDMI-A-1 enable
	swaymsg output HDMI-A-1 mode "${VG27W_MODE}" pos 0 0
	swaymsg output DP-3 enable
	swaymsg output DP-3 mode "${BENQ_MODE}" pos 2560 0
	swaymsg output DP-4 disable
elif [ "$1" == "salon" ]; then
	swaymsg output DP-4 enable
	swaymsg output DP-4 mode "${VG27A_MODE}" pos 5120 0
	swaymsg output HDMI-A-1 disable
	swaymsg output DP-3 disable
else
	echo "imdat"
fi
