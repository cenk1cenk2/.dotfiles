#!/usr/bin/env bash

(yabai -m query --spaces --display |
	jq -re 'map(select(."is-native-fullscreen" == false)) | length <= 1' &&
	yabai -m space --create)

yabai -m space --display "$1"
yabai -m display --focus "$1"
