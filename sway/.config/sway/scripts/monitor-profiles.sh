#!/bin/bash

# Use swaymsg if WAYLAND_DISPLAY is set
WM_MSG=${WAYLAND_DISPLAY+swaymsg}
WM_MSG=${WM_MSG:-i3-msg}

MAIN_MODE="3840x1600@74.997Hz"
MAIN_OUTPUT="Goldstar Company Ltd 38GN950 207NTSU62014"
TOP_MODE="2560x1440@164.999Hz"
TOP_OUTPUT="Unknown VG27A L4LMQS123005"

SALON_MODE="2560x1440@143.995Hz"
SALON_OUTPUT="Unknown VG27WQ L4LMDW007740"

if [ "$1" == "solo" ]; then
	swaymsg output "'$MAIN_OUTPUT'" enable
	swaymsg output "'$MAIN_OUTPUT'" mode "$MAIN_MODE" pos 0 0

	swaymsg output "'$TOP_OUTPUT'" disable
	swaymsg output "'$SALON_OUTPUT'" disable
elif [ "$1" == "top" ]; then
	swaymsg output "'$TOP_OUTPUT'" enable
	swaymsg output "'$TOP_OUTPUT'" mode "$TOP_MODE" pos 700 0

	swaymsg output "'$MAIN_OUTPUT'" disable
	swaymsg output "'$SALON_OUTPUT'" disable
elif [ "$1" == "dual" ]; then
	swaymsg output "'$MAIN_OUTPUT'" enable
	swaymsg output "'$MAIN_OUTPUT'" mode "$MAIN_MODE" pos 0 1440
	swaymsg output "'$TOP_OUTPUT'" enable
	swaymsg output "'$TOP_OUTPUT'" mode "$TOP_MODE" pos 700 0

	swaymsg output "'$SALON_OUTPUT'" disable
elif [ "$1" == "salon" ]; then
	swaymsg output "'$SALON_OUTPUT'" enable
	swaymsg output "'$SALON_OUTPUT'" mode "$SALON_MODE" pos 0 0

	swaymsg output "'$MAIN_OUTPUT'" disable
	swaymsg output "'$TOP_OUTPUT'" disable
else
	echo "imdat"
fi
