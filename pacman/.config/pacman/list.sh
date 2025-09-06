#!/usr/bin/env zsh

LOAD="${1:-dump.txt}"

echo "Dumping packages to: $LOAD"
yay -Qqe >"$LOAD"
