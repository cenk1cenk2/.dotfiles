#!/usr/bin/env zsh

LOAD="${1}"

if [[ -z "$LOAD" ]]; then
  echo "Usage: $0 <package_list_file> [additional_packages...]"
  exit 1
fi
shift

echo "Removing packages from: $LOAD"

cat "$LOAD"

yay -R "${@}" - <"$LOAD"
