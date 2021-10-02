#!/bin/bash
# Based on https://github.com/sandeel/i3-new-workspace
# Script occupies free workspace with lowest possible number

# Use swaymsg if WAYLAND_DISPLAY is set
WM_MSG=${WAYLAND_DISPLAY+swaymsg}
WM_MSG=${WM_MSG:-i3-msg}

WS_JSON=$($WM_MSG -t get_workspaces)

CURRENT_WORKSPACE=$(echo $WS_JSON \
	| jq '.[] | select(.focused==true).name' \
	| cut -d"\"" -f2 | cut -d ":" -f1)

if [ "$1" == "left" ]; then
	PREVIOUS_WORKSPACE=$((CURRENT_WORKSPACE - 1))
	$WM_MSG rename workspace number $PREVIOUS_WORKSPACE to $CURRENT_WORKSPACE
	$WM_MSG rename workspace to $PREVIOUS_WORKSPACE
	$WM_MSG workspace $PREVIOUS_WORKSPACE

elif [ "$1" == "right" ]; then
	NEXT_WORKSPACE=$((CURRENT_WORKSPACE + 1))
	$WM_MSG rename workspace number $NEXT_WORKSPACE to $CURRENT_WORKSPACE
	$WM_MSG rename workspace to $NEXT_WORKSPACE
	$WM_MSG workspace $NEXT_WORKSPACE

else
	$WM_MSG rename workspace number $1 to $CURRENT_WORKSPACE
	$WM_MSG rename workspace to $1
	$WM_MSG workspace $1

fi
