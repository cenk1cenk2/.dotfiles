#!/bin/sh
# Compositor-agnostic scratchpad script for waybar

if [ -n "$HYPRLAND_INSTANCE_SIGNATURE" ]; then
    # Hyprland: Get windows in special:scratch workspace
    tooltip=$(hyprctl clients -j | jq -r '.[] | select(.workspace.name == "special:scratch") | "\(.class) | \(.title)"')
    count=$(echo -n "$tooltip" | grep -c '^' || echo 0)
elif [ -n "$SWAYSOCK" ]; then
    # Sway: Get windows in scratchpad
    tooltip=$(swaymsg -r -t get_tree | jq -r 'recurse(.nodes[]) | first(select(.name=="__i3_scratch")) | .floating_nodes | .[] | "\(.app_id) | \(.name)"')
    count=$(echo -n "$tooltip" | grep -c '^' || echo 0)
else
    count=0
    tooltip=""
fi

if [[ "$count" -eq 0 ]]; then
	exit 1
elif [[ "$count" -eq 1 ]]; then
	class="one"
elif [[ "$count" -gt 1 ]]; then
	class="many"
else
	class="unknown"
fi

printf '{"text":"%s", "class":"%s", "alt":"%s", "tooltip":"%s"}\n' "$count" "$class" "$class" "${tooltip//$'\n'/'\n'}"
