#!/bin/bash
# Auto-install Hyprland plugins if not already installed

# List of plugins to install: "name|repo_url"
PLUGINS=(
  "hy3|https://github.com/outfoxxed/hy3"
)

for plugin_entry in "${PLUGINS[@]}"; do
  IFS='|' read -r plugin_name plugin_url <<< "$plugin_entry"
  
  # Check if plugin is already in hyprpm list
  if ! hyprpm list 2>/dev/null | grep -q "$plugin_url"; then
    echo "Installing $plugin_name from $plugin_url..."
    hyprpm add "$plugin_url"
    hyprpm enable "$plugin_name"
  fi
done

# Reload plugins
hyprpm reload -n
