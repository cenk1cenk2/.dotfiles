#!/usr/bin/env zsh

local dir=$(mktemp -d)
git clone https://github.com/boydaihungst/xdg-desktop-portal-termfilechooser.git $dir
cd $dir
meson build && ninja -C build && sudo ninja -C build install
sudo cp /usr/local/share/xdg-desktop-portal/portals/termfilechooser.portal /usr/share/xdg-desktop-portal/portals/
sudo rm -r $dir
