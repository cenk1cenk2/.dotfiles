#!/usr/bin/env bash

# set -u

declare -A signal
signal[github]=4
signal[playerctl]=5
signal[sunset]=6
signal[scratchpad]=7
signal[recorder]=8
signal[clipboard]=9
signal[zeit]=10
signal[dnd]=11
signal[adaptive_brightness]=12
signal[valent]=13
signal[pacman]=14
signal[idle]=15

number=${signal[${1}]}

if [[ -z "$number" ]]; then
	echo "No signal mapped for $1"
	exit 1
elif [[ ! "$(pgrep -x waybar)" ]]; then
	echo "Waybar not available."
	exit 0
fi

echo "Sending waybar signal: $number for $1"

pkill -x -SIGRTMIN+${number} 'waybar'
