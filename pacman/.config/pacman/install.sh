#!/usr/bin/env bash

LOAD=${1:-minimum.txt}

echo "Loading packages from: $LOAD"

cat "$LOAD"

yay -S --needed --noconfirm --overwrite '*' - <"$LOAD"
