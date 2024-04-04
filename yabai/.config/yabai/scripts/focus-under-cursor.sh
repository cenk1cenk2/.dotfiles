#!/usr/bin/env bash

if yabai -m query --windows --space |
	jq -er 'map(select(.focused == 1)) | length == 0' >/dev/null; then
	yabai -m window --focus mouse 2>/dev/null || true
fi
