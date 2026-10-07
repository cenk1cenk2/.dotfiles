#!/bin/sh
# Follows one watcher's $XDG_RUNTIME_DIR/waybar/<name>.json for a bar module.
# tail -F only follows a file that exists when it starts, so seed an empty
# text (which hides the module) if the watcher has not written one yet.

f="$XDG_RUNTIME_DIR/waybar/$1.json"
mkdir -p "${f%/*}"
[ -e "$f" ] || echo '{"text": ""}' >"$f"
exec tail -F -n1 "$f" 2>/dev/null
