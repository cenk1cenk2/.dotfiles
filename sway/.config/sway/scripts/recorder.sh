#!/usr/bin/env bash

pgrep wl-screenrec
status=$?

if [ "$1" == "kill" ] && [ $status == 0 ]; then
  killall -s SIGINT wl-screenrec
  waybar-signal.sh recorder
  exit 0
elif [ $status == 0 ]; then
  notify-send "Recording already in progress." -i /usr/share/icons/Papirus-Dark/32x32/devices/camera-video.svg
  exit 1
fi

countdown() {
  for i in $(seq 3); do
    notify-send "Recording in $((3 + 1 - i)) seconds." -t 1000
    sleep 1
  done
}

notify() {
  line=$1
  shift
  notify-send "Recording..." "${line}" -i /usr/share/icons/Papirus-Dark/32x32/devices/camera-video.svg "$@"
}

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

notify-send "Finished recording: ${file}"
