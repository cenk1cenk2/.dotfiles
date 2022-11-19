#!/usr/bin/env bash

sudo pacman -S --needed base-devel
sudo pacman -S --needed - <pkglist.txt
yay -S --needed - <pkglist_aur.txt
