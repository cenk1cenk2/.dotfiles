#!/usr/bin/env sh

uid="$(cat /tmp/wayland-uid)"
user="$(cat /tmp/wayland-user)"

export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus"

eval "su '$user' -c '$1'"
