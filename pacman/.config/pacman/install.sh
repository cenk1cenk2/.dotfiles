#!/usr/bin/env zsh

LOAD=${1:-minimum.txt}

echo "Loading packages from: $LOAD"

cat "$LOAD"

yay -S --needed --noconfirm --overwrite '*' - <"$LOAD"

case "$LOAD" in
gaming.txt)
  sudo setcap 'CAP_SYS_NICE=eip' "$(which gamescope)"
  ;;
*)
  echo "Nothing to perform additionally."
  ;;
esac
