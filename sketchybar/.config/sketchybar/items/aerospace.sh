#!/usr/bin/env bash

sketchybar --add event aerospace_workspace_change

# Destroy space on right click, focus space on left click.
# New space by left clicking separator (>)

for sid in $(aerospace list-workspaces --monitor all --empty no); do

	space=(
		space=$sid
		icon="${SPACE_ICONS[i]}"
		icon.padding_left=10
		icon.padding_right=10
		padding_left=2
		padding_right=2
		label.padding_right=20
		icon.highlight_color=$RED
		label.color=$GREY
		label.highlight_color=$WHITE
		label.font="sketchybar-app-font:Regular:16.0"
		label.y_offset=-1
		background.color=$BACKGROUND_1
		background.border_color=$BACKGROUND_2
		click_script="aerospace workspace $sid"
		script="$PLUGIN_DIR/space.sh $sid"
	)

	sketchybar --add space space.$sid left \
		--set space.$sid "${space[@]}" \
		--subscribe space.$sid aerospace_workspace_change \
		--subscribe space.$sid mouse.clicked
done
