#!/bin/sh
# wrapper script for foot

USER_CONFIG_PATH="${HOME}/.config/foot/foot.ini"

foot -c "${USER_CONFIG_PATH}" "$@" &
