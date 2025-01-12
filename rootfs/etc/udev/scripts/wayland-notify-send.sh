#!/usr/bin/env sh

su "$(cat /tmp/wayland-user)" -c "notify-send $*"
