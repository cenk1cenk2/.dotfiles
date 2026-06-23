-- Hyprland configuration (Lua, 0.55+)
-- Modular structure inspired by Sway configuration.
-- Renamed from hyprland.lua so the legacy hyprland.conf keeps loading
-- on 0.54. To activate: rename this to `hyprland.lua`.

local config_dir = ("%s/.config/hypr"):format(os.getenv("HOME"))

package.path = table.concat({
  ("%s/?.lua"):format(config_dir),
  ("%s/?/?.lua"):format(config_dir),
  ("%s/config.d/?.lua"):format(config_dir),
  package.path,
}, ";")

table.insert(package.searchers, function(name)
  local path = package.searchpath(name, package.path, ".", ".")
  if path then
    return loadfile(path)
  end
end)

-- Environment variables are prepared by UWSM for UWSM-managed sessions.
-- Keep compositor/session/toolkit variables in ~/.config/uwsm/env* so they
-- are available before Hyprland and graphical-session.target start.

-- Load shared definitions (theme first, since theme exposes colors used
-- by 90-theming). Modes/config.d require definitions themselves; the
-- entry doesn't reference it directly so it's not bound here.
local theme = require("themes.custom.definitions")

-- Source plugin definitions
require("plugins")

-- Source input + output configurations (via per-directory init.lua)
require("inputs")
require("outputs")

-- Base configuration settings
hl.config({
  general = {
    border_size = 2,
    gaps_in = 2,
    gaps_out = 2,
    layout = "dwindle",
    resize_on_border = true,
    extend_border_grab_area = 15,
    allow_tearing = true,
  },

  decoration = {
    rounding = 0,
    active_opacity = 0.95,
    inactive_opacity = 0.95,
    dim_inactive = false,
    dim_strength = 0.2,

    blur = {
      enabled = true,
      size = 8,
      passes = 2,
      noise = 0.01,
      contrast = 1.5,
      brightness = 1.2,
      xray = false,
      new_optimizations = true,
    },

    shadow = {
      enabled = true,
      range = 2,
      render_power = 2,
    },
  },

  animations = {
    enabled = true,
  },

  group = {
    col = {
      border_active = theme.colors[3],
      border_inactive = theme.colors[8],
    },

    groupbar = {
      font_size = 12,
      height = 24,
      render_titles = true,
      text_color = theme.colors[16],
      col = {
        active = theme.colors[3],
        inactive = theme.colors[8],
      },
    },
  },

  dwindle = {
    -- `pseudotile` removed in 0.55 (was a no-op for several releases);
    -- pseudo behaviour is now per-window via the `pseudo` window-rule
    -- action or the `togglepseudo` dispatcher.
    preserve_split = true,
    force_split = 2,
    smart_split = false,
    smart_resizing = true,
  },

  master = {
    new_status = "master",
    new_on_top = true,
  },

  misc = {
    disable_hyprland_logo = true,
    disable_autoreload = true,
    disable_splash_rendering = true,
    force_default_wallpaper = 0,
    mouse_move_enables_dpms = true,
    key_press_enables_dpms = true,
  },

  xwayland = {
    force_zero_scaling = false,
  },
})

-- Global animation
hl.animation({ leaf = "global", enabled = true, speed = 0.5, bezier = "default" })

-- Disable borders and gaps when only one tiled window in workspace
hl.workspace_rule({ workspace = "w[tv1]", gaps_out = 0, gaps_in = 0 })

-- Source config.d/ and modes/ (each via its init.lua re-exports).
require("config.d")
require("modes")
