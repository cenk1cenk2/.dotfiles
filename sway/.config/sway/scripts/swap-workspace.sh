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

$WM_MSG rename workspace number $1 to $CURRENT_WORKSPACE
$WM_MSG rename workspace to $1
