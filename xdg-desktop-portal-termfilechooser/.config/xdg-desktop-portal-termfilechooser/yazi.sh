#!/usr/bin/env sh

save="$3"
path="$4"
out="$5"

if [ "$save" = "1" ]; then
  # read by the yazi `g s` keybind to jump back to the save target
  export TERMFILECHOOSER_SAVE_PATH="$path"

  # yazi only reveals a path that exists, so the suggested save target gets a placeholder
  if [ ! -e "$path" ]; then
    : >"$path"
    trap '[ "$(cat "$out" 2>/dev/null)" = "$path" ] || rm -f "$path"' EXIT
  fi
fi

/usr/share/xdg-desktop-portal-termfilechooser/yazi-wrapper.sh "$@"
