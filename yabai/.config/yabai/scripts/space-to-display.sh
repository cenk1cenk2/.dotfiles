#!/usr/bin/env bash

(yabai -m query --spaces --display |
	jq -re 'map(select(."is-native-fullscreen" == false)) | length <= 1' &&
	yabai -m space --create)

window_id="$(yabai -m query --windows --display | jq -re 'first(.[] | select(.["has-focus"] == true) | .id)')"

yabai -m space --display "$1"

yabai -m display --focus "$(yabai -m query --windows | jq -re '.[] | select(.id == '"$window_id"') | .display')"

yabai -m window --focus "$window_id"
