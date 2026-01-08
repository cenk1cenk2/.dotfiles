#!/usr/bin/env bash

set -e

case "$1" in
"help")
  echo "$(
    cat <<EOF
$0

List available profiles.
Usage: $0 ls

Reload the configuration.
Usage: $0 reload

Apply a profile.
Usage: $0 [profile]
EOF
  )"
  ;;
"reload")
  kanshictl reload
  ;;
"ls")
  cat "$HOME/.config/kanshi/config" | grep -E '^profile ' | awk '{print $2}' | uniq
  ;;
*)
  kanshictl switch "$1"
  # notify-send "display" "Applied profile $1." -i /usr/share/icons/Adwaita/scalable/devices/video-display.svg
  ;;
esac
