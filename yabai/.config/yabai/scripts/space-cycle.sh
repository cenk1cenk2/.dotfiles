#!/usr/bin/env bash

info=$(yabai -m query --spaces --display)

case "$1" in
"next")
	last=$(echo $info | jq '.[-1]."has-focus"')

	if [[ $last == "false" ]]; then
		yabai -m space --focus next
	else
		yabai -m space --focus "$(echo $info | jq '.[0].index')"
	fi
	;;
"prev")
	first=$(echo $info | jq '.[0]."has-focus"')

	if [[ $first == "false" ]]; then
		yabai -m space --focus prev
	else
		yabai -m space --focus "$(echo $info | jq '.[-1].index')"
	fi
	;;
*)
	echo "Invalid argument. Use 'first' or 'last' as argument."
	exit 127
	;;
esac
