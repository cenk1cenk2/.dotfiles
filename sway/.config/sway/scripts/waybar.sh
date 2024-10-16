#!/usr/bin/env bash
# wrapper script for waybar with args, see https://github.com/swaywm/sway/issues/5724

pkill -x waybar

USER_CONFIG_PATH=$HOME/.config/waybar/config.jsonc
USER_STYLE_PATH=$HOME/.config/waybar/style.css

waybar -c ${USER_CONFIG_PATH} -s ${USER_STYLE_PATH} >$(mktemp -t XXXX.waybar.log) 2>&1
