-- GTK Theme and Font Configuration

local theme = require("themes.custom.definitions")

-- xsettingsd: hl.on("hyprland.start", ...) doesn't fire in 0.55, so
-- start at module top. Guard with pidof so hyprctl reload doesn't
-- spawn a duplicate.
hl.exec_cmd("pidof xsettingsd >/dev/null || xsettingsd &")

hl.exec_cmd(("gsettings set org.gnome.desktop.interface gtk-theme '%s'"):format(theme.gtk.theme))
hl.exec_cmd(("gsettings set org.gnome.desktop.interface icon-theme '%s'"):format(theme.gtk.icon_theme))
hl.exec_cmd(("gsettings set org.gnome.desktop.interface cursor-theme '%s'"):format(theme.gtk.cursor_theme))

-- Hyprcursor with XCursor fallback
hl.env("HYPRCURSOR_THEME", theme.gtk.cursor_theme)
hl.env("HYPRCURSOR_SIZE", "24")
hl.env("XCURSOR_THEME", theme.gtk.cursor_theme)
hl.env("XCURSOR_SIZE", "24")

hl.config({
  cursor = {
    no_hardware_cursors = false,
    enable_hyprcursor = true,
  },
})

hl.exec_cmd(("gsettings set org.gnome.desktop.interface font-name '%s'"):format(theme.font.gui))
hl.exec_cmd(("gsettings set org.gnome.desktop.interface monospace-font-name '%s'"):format(theme.font.term))
-- Fontconfig aliases so CSS `font-family: monospace` / `sans-serif`
-- resolve to the same families (matters for GTK4 apps whose CSS names
-- a generic family — GTK doesn't consult GSettings for that).
hl.exec_cmd(("~/.config/wayland/scripts/gtk-config.sh font monospace '%s'"):format(theme.font.term))
hl.exec_cmd(("~/.config/wayland/scripts/gtk-config.sh font sans-serif '%s'"):format(theme.font.gui))
hl.exec_cmd("gsettings set org.gnome.desktop.input-sources show-all-sources true")
hl.exec_cmd(("gsettings set org.freedesktop.appearance color-scheme '%s'"):format(theme.gtk.color_scheme))
hl.exec_cmd(("gsettings set org.gnome.desktop.interface color-scheme '%s'"):format(theme.gtk.color_scheme))
hl.exec_cmd("gsettings set org.gnome.desktop.interface gtk-key-theme 'Default'")

-- Hyprland colors using theme variables
hl.config({
  general = {
    col = {
      active_border = theme.colors[3],
      inactive_border = theme.colors[8],
    },
  },
  decoration = {
    shadow = {
      color = "rgba(1a1a1aee)",
    },
  },
  -- Group (tabbed) colors
  group = {
    col = {
      border_active = theme.colors[3],
      border_inactive = theme.colors[8],
    },
    groupbar = {
      col = {
        active = theme.colors[3],
        inactive = theme.colors[8],
      },
    },
  },
})
