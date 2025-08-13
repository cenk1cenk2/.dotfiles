#!/usr/bin/env bash

PATH=/usr/bin

# Send an alert to all graphical users.
for ADDRESS in /run/user/*; do
  USERID=${ADDRESS#/run/user/}
  /usr/bin/sudo -u "#$USERID" DBUS_SESSION_BUS_ADDRESS="unix:path=$ADDRESS/bus" /usr/bin/notify-send "${@:1}"
done
