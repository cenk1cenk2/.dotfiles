#!/bin/bash
# Compositor-agnostic command executor
# Automatically detects whether running Sway or Hyprland and executes accordingly

if [ -n "$HYPRLAND_INSTANCE_SIGNATURE" ]; then
    # Running Hyprland
    hyprctl dispatch exec "$@"
elif [ -n "$SWAYSOCK" ]; then
    # Running Sway
    swaymsg exec "$@"
else
    # Fallback to direct execution
    exec "$@"
fi
