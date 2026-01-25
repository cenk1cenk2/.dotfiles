#!/bin/bash
# Monitor playerctl and signal waybar on metadata changes

[[ -x "$(command -v playerctl)" ]] || exit 0

pkill playerctl
playerctl --player=spotify -a metadata --format '{{status}} {{title}}' --follow | while read line; do pkill -RTMIN+5 waybar; done &
