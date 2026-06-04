-- GTK Theme and Font Configuration

local theme = require("themes.custom.definitions")

-- Hyprcursor with XCursor fallback
-- Cursor theme — propagate so Qt/GTK apps spawned by systemd pick
-- up the same theme as Hyprland-spawned ones.
hl.env("HYPRCURSOR_THEME", theme.gtk.cursor_theme, true)
hl.env("HYPRCURSOR_SIZE", "24", true)
hl.env("XCURSOR_THEME", theme.gtk.cursor_theme, true)
hl.env("XCURSOR_SIZE", "24", true)

hl.config({
  cursor = {
    no_hardware_cursors = false,
    enable_hyprcursor = true,
  },
})

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
