#!/usr/bin/env bash

POPUP_OFF='sketchybar --set apple.logo popup.drawing=off'
POPUP_CLICK_SCRIPT='sketchybar --set $NAME popup.drawing=toggle'

apple_logo=(
	icon=$APPLE
	icon.font="$FONT:Black:16.0"
	icon.color=$GREEN
	padding_right=15
	label.drawing=off
	click_script="$POPUP_CLICK_SCRIPT"
	popup.height=35
)

apple_prefs=(
	icon=$PREFERENCES
	label="Preferences"
	click_script="open -a 'System Preferences'; $POPUP_OFF"
)

apple_activity=(
	icon=$ACTIVITY
	label="Activity"
	click_script="open -a 'Activity Monitor'; $POPUP_OFF"
)

apple_lock=(
	icon=$LOCK
	label="Sleep"
	click_script="pmset sleepnow; $POPUP_OFF"
)

apple_restart=(
	icon=$LOCK
	label="Restart"
	click_script="shutdown -r -h now; $POPUP_OFF"
)

apple_shutdown=(
	icon=$LOCK
	label="Shutdown"
	click_script="shutdown -h now; $POPUP_OFF"
)

sketchybar --add item apple.logo left \
	--set apple.logo "${apple_logo[@]}" \
	\
	--add item apple.prefs popup.apple.logo \
	--set apple.prefs "${apple_prefs[@]}" \
	\
	--add item apple.activity popup.apple.logo \
	--set apple.activity "${apple_activity[@]}" \
	\
	--add item apple.lock popup.apple.logo \
	--set apple.lock "${apple_lock[@]}" \
	\
	--add item apple.restart popup.apple.logo \
	--set apple.restart "${apple_restart[@]}" \
	\
	--add item apple.shutdown popup.apple.logo \
	--set apple.shutdown "${apple_shutdown[@]}"
