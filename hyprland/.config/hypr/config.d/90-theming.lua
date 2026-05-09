-- GTK Theme and Font Configuration

local theme = require("themes.custom.definitions")

hl.on("hyprland.start", function()
  hl.exec_cmd("xsettingsd")
end)

hl.exec_cmd(string.format("gsettings set org.gnome.desktop.interface gtk-theme '%s'", theme.gtk.theme))
hl.exec_cmd(string.format("gsettings set org.gnome.desktop.interface icon-theme '%s'", theme.gtk.icon_theme))
hl.exec_cmd(string.format("gsettings set org.gnome.desktop.interface cursor-theme '%s'", theme.gtk.cursor_theme))

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

hl.exec_cmd(string.format("gsettings set org.gnome.desktop.interface font-name '%s'", theme.font.gui))
hl.exec_cmd(string.format("gsettings set org.gnome.desktop.interface monospace-font-name '%s'", theme.font.term))
-- Fontconfig aliases so CSS `font-family: monospace` / `sans-serif`
-- resolve to the same families (matters for GTK4 apps whose CSS names
-- a generic family — GTK doesn't consult GSettings for that).
hl.exec_cmd(string.format("~/.config/wayland/scripts/gtk-config.sh font monospace '%s'", theme.font.term))
hl.exec_cmd(string.format("~/.config/wayland/scripts/gtk-config.sh font sans-serif '%s'", theme.font.gui))
hl.exec_cmd("gsettings set org.gnome.desktop.input-sources show-all-sources true")
hl.exec_cmd(string.format("gsettings set org.freedesktop.appearance color-scheme '%s'", theme.gtk.color_scheme))
hl.exec_cmd(string.format("gsettings set org.gnome.desktop.interface color-scheme '%s'", theme.gtk.color_scheme))
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
