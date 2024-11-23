#!/usr/bin/env bash

set -e

if [ "$1" == "ls" ]; then
  echo "Available sources are listed below."

  echo 'media.class,alsa.card_name,media.name'
  echo '-------------------------------------'

  pw-dump | jq -r '.[] | select(.type == "PipeWire:Interface:Node") | [ .info.props["media.class"], .info.props["alsa.card_name"], .info.props["media.name"]] | @csv'
  exit 0
fi

# to inspect current
# pw-dump | jq --arg media_class "Audio/Sink" --arg card_name "Scarlett 8i6 USB" '.[] | select(.type == "PipeWire:Interface:Node")' | b -l json
wpctl set-default $(pw-dump | jq --arg media_class "$1" --arg card_name "$2" '.[] | select(.type == "PipeWire:Interface:Node" and ([.info.props["alsa.card_name"], .info.props["media.name"]] | index($card_name) > -1) and .info.props["media.class"] == $media_class) | .id')
notify-send "wireplumber [$1]" "$2" -i /usr/share/icons/Adwaita/scalable/devices/audio-headphones.svg
