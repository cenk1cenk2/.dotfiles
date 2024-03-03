#!/usr/bin/env bash

URL=$(goploader $1)
echo $URL | wl-copy
notify-send " $URL"
