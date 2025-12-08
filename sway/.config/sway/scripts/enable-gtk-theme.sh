#!/usr/bin/env bash

THEME=$1

gsettings set org.gnome.desktop.interface gtk-theme $THEME

mkdir -p "$HOME"/.config/gtk-4.0
if [ -d "$HOME/.themes/${THEME}" ]; then
  THEME_DIR="$HOME/.themes/${THEME}/gtk-4.0"
# if [ -d "/usr/share/themes/${THEME}" ]; then
else
  THEME_DIR="/usr/share/themes/${THEME}/gtk-4.0"
fi
CONFIG_DIR="$HOME/.config/gtk-4.0/"

[ -d "$THEME_DIR/assets" ] && cp -rf --backup "${THEME_DIR}/assets" "$CONFIG_DIR"
[ -f "$THEME_DIR/gtk.css" ] && cp -rf --backup "${THEME_DIR}/gtk.css" "$CONFIG_DIR"

[ -f "$THEME_DIR/gtk-dark.css" ] && cp -rf --backup "${THEME_DIR}/gtk-dark.css" "$CONFIG_DIR"
[ ! -f "$THEME_DIR/gtk-dark.css" ] && rm -rf "$CONFIG_DIR/gtk-dark.css"

[ -d "$THEME_DIR/icons" ] && cp -rf --backup "${THEME_DIR}/icons" "$CONFIG_DIR"

# sed -Ei 's/^(gtk-application-prefer-dark-theme=).*$/\1true/' "$CONFIG_DIR/settings.ini"
