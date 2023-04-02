#!/bin/bash
set -x

pid=$(pgrep wf-recorder)
status=$?

countdown() {
	notify "Recording in 3 seconds." -t 1000
	sleep 1
	notify "Recording in 2 seconds." -t 1000
	sleep 1
	notify "Recording in 1 seconds." -t 1000
	sleep 1
}

notify() {
	line=$1
	shift
	notify-send "Recording..." "${line}" -i /usr/share/icons/Papirus-Dark/32x32/devices/camera-video.svg $*
}

if [ $status != 0 ]; then
	target_path=$(xdg-user-dir VIDEOS)
	timestamp=$(date +'recording_%Y%m%d-%H%M%S')

	if [ "$1" == "mp4" ]; then
		file="$target_path/$timestamp.mp4"
		command="wf-recorder --file='$file'"
	else
		file="$target_path/$timestamp.webm"
		command="wf-recorder -c libvpx --codec-param='qmin=0' --codec-param='qmax=25' --codec-param='crf=4' --codec-param='b:v=1M' --file='$file'"
	fi

	if [ "$2" == "region" ]; then
		notify "Select a region to record" -t 1000
		area=$(swaymsg -t get_tree | jq -r '.. | select(.pid? and .visible?) | .rect | "\(.x),\(.y) \(.width)x\(.height)"' | slurp)
		command="$command -g '$area'"
	fi

	if [ "$3" == "audio" ]; then
		command="$command -a"
	fi

	countdown
	(sleep 0.5 && pkill -RTMIN+8 waybar) &

	eval "$command"

	pkill -RTMIN+8 waybar && notify "Finished recording: ${file}"
else
	pkill --signal SIGINT wf-recorder
	pkill -RTMIN+8 waybar
fi
