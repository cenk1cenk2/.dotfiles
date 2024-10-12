#!/usr/bin/env bash

notify() {
  line=$1
  shift
  notify-send "Recording..." "${line}" -i /usr/share/icons/Adwaita/scalable/devices/camera-web.svg "$@"
}

countdown() {
  for i in $(seq 3); do
    notify "Recording in $((3 + 1 - i)) seconds." -t 1000
    sleep 1
  done
}

pgrep wl-screenrec
status=$?

if [ "$1" == "kill" ]; then
  killall -s SIGINT wl-screenrec
  waybar-signal.sh recorder
  notify "Recording stopped."
  exit 0
elif [ $status == 0 ]; then
  notify "Recording already in progress."
  exit 1
fi

target_path=$(xdg-user-dir VIDEOS)
timestamp=$(date +'recording_%Y%m%d-%H%M%S')

file="$target_path/$timestamp.$1"
command="wl-screenrec -f='$file' --codec hevc"

if [ "$2" == "region" ]; then
  notify "Select a region to record" -t 1000
  area=$(swaymsg -t get_tree | jq -r '.. | select(.pid? and .visible?) | .rect | "\(.x),\(.y) \(.width)x\(.height)"' | slurp)
  command="$command -g '$area'"
fi

if [ "$3" == "audio" ]; then
  command="$command --audio"
fi

countdown

eval "$command"

notify "Finished recording: ${file}"
