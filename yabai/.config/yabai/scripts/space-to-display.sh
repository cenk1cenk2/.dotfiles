#!/usr/bin/env bash

(yabai -m query --spaces --display |
	jq -re 'map(select(."is-native-fullscreen" == false)) | length <= 1' &&
	yabai -m space --create)

window_id="$(yabai -m query --windows --display | jq -re 'first(.[] | select(.["has-focus"] == true) | .id)')"

yabai -m space --display "$1"

space_id="$(yabai -m query --windows | jq -re --arg window_id $window_id 'first(.[] | select(.id | tostring == $window_id) | .space)')"

yabai -m space --focus "$space_id"
yabai -m window --focus "$window_id"
