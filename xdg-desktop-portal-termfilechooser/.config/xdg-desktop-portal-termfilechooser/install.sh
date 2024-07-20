#!/usr/bin/env zsh

local dir=$(mktemp -d)
git clone https://github.com/boydaihungst/xdg-desktop-portal-termfilechooser.git $dir
cd $dir
meson build && ninja -C build && sudo ninja -C build install
sudo rm -r $dir
