#!/usr/bin/env bash

wpctl set-default $(jq --arg card_name="$1" '.[] | select(.type == "PipeWire:Interface:Device") | select(.info.props["alsa.card_name"] == "$card_name") | .id')
