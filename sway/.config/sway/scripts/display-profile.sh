#!/usr/bin/env bash

set -e

if [ "$1" == "reload" ]; then
  shikanectl reload
fi

shikanectl switch "$1"

# notify-send "display" "Applied profile $1." -i /usr/share/icons/Adwaita/scalable/devices/video-display.svg
