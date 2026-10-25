#!/usr/bin/env sh

save="$3"
path="$4"
out="$5"

# yazi only reveals a path that exists, so the suggested save target gets a placeholder
if [ "$save" = "1" ] && [ ! -e "$path" ]; then
  : >"$path"
  trap '[ "$(cat "$out" 2>/dev/null)" = "$path" ] || rm -f "$path"' EXIT
fi

/usr/share/xdg-desktop-portal-termfilechooser/yazi-wrapper.sh "$@"
