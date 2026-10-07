#!/bin/sh
# Publishes one status watcher's lines to $XDG_RUNTIME_DIR/waybar/<name>.json
# for every bar to `tail -F`, so a module costs one watcher however many bars
# show it. Run by waybar-watch@<name>.service.
#
# Each line is written to a temp file and renamed over the target: tail -F
# follows the rename and never sees a half-written line.

out="$XDG_RUNTIME_DIR/waybar/$1.json"

case "$1" in
  stt) set -- "$HOME/.config/wayland/scripts/speech.py" stt status --watch ;;
  tts) set -- "$HOME/.config/wayland/scripts/speech.py" tts status --watch ;;
  copywriter) set -- "$HOME/.config/wayland/scripts/copywriter.py" status --watch ;;
  recorder) set -- "$HOME/.config/wayland/scripts/recorder.py" status --watch ;;
  zoom) set -- "$HOME/.config/hypr/scripts/zoom.py" status --watch ;;
  gpu)
    set -- sh -c 'nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader,nounits -lms 5000 |
      awk '"'"'{c=($1>=72)?"critical":(($1>=62)?"warning":""); printf "{\"text\":\"%d\",\"class\":\"%s\"}\n",$1,c; fflush()}'"'"''
    ;;
  *)
    echo "unknown watcher: $1" >&2
    exit 2
    ;;
esac

"$@" | while IFS= read -r line; do
  printf '%s\n' "$line" >"$out.tmp" && mv -f "$out.tmp" "$out"
done
