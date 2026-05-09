-- Hyprland configuration (Lua, 0.55+)
-- Modular structure inspired by Sway configuration.
-- Renamed from hyprland.lua so the legacy hyprland.conf keeps loading
-- on 0.54. To activate: rename this to `hyprland.lua`.

local config_dir = os.getenv("HOME") .. "/.config/hypr"

-- Make sibling .lua files reachable via require().
package.path = table.concat({
  config_dir .. "/?.lua",
  config_dir .. "/?/init.lua",
  package.path,
}, ";")

-- Glob loader: dofile every *.lua in `rel` (alphabetical order).
-- Mirrors hyprlang's `source = path/*.conf` behavior.
local function source_dir(rel)
  local handle = io.popen("ls -1 " .. config_dir .. "/" .. rel .. "/*.lua 2>/dev/null | sort")
  if not handle then
    return
  end
  for path in handle:lines() do
    dofile(path)
  end
  handle:close()
end

-- Environment variables
hl.env("XDG_SESSION_TYPE", "wayland")
hl.env("XDG_CURRENT_DESKTOP", "Hyprland")

-- Libseat backend
hl.env("LIBSEAT_BACKEND", "logind")

-- WLR settings
-- hl.env("WLR_RENDERER_ALLOW_SOFTWARE", "1")
hl.env("WLR_XWAYLAND", "/usr/local/bin/Xwayland")

-- Qt settings
hl.env("QT_QPA_PLATFORM", "wayland")
hl.env("QT_QPA_PLATFORMTHEME", "gtk3")
hl.env("QT_WAYLAND_DISABLE_WINDOWDECORATION", "1")
hl.env("QT_QUICK_CONTROLS_STYLE", "org.hyprland.style")

-- GTK settings
hl.env("GTK_USE_PORTAL", "1")
hl.env("GDK_BACKEND", "wayland")
hl.env("GDK_DEBUG", "portals")
hl.env("ELECTRON_OZONE_PLATFORM_HINT", "wayland")

-- Gaming optimizations
hl.env("PROTON_DXVK_LOWLATENCY", "1")
hl.env("MANGOHUD", "1")
hl.env("VKD3D_CONFIG", "dxr,dxr11")
hl.env("VKD3D_FEATURE_LEVEL", "12_2")
hl.env("PROTON_ENABLE_WAYLAND", "1")
hl.env("DXVK_HDR", "1")
hl.env("PROTON_ENABLE_HDR", "1")

-- Firefox/Mozilla
hl.env("MOZ_DBUS_REMOTE", "1")

-- Add mise shims and user bin to PATH
hl.env("PATH", os.getenv("HOME") .. "/.local/share/mise/shims:" .. os.getenv("HOME") .. "/.local/bin:" .. (os.getenv("PATH") or ""))

-- Load shared definitions (theme first, since theme exposes colors used by 90-theming)
local theme = require("themes.custom.definitions")
local d = require("definitions")

-- Source plugin definitions
require("plugins")

-- Source input configurations
source_dir("inputs")

-- Source output configurations
source_dir("outputs")

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
    pseudotile = true,
    preserve_split = true,
    force_split = 2,
    smart_split = false,
    smart_resizing = true,
  },

  master = {
    new_status = "master",
    new_on_top = true,
  },

  gestures = {
    workspace_swipe_distance = 300,
    workspace_swipe_cancel_ratio = 0.5,
  },

  misc = {
    disable_hyprland_logo = true,
    disable_splash_rendering = true,
    force_default_wallpaper = 0,
    mouse_move_enables_dpms = true,
    key_press_enables_dpms = true,
  },

  xwayland = {
    force_zero_scaling = false,
  },
})

-- 3-finger horizontal swipe to switch workspaces
hl.gesture({ fingers = 3, direction = "horizontal", action = "workspace" })

-- Global animation
hl.animation({ leaf = "global", enabled = true, speed = 0.5, bezier = "default" })

-- Disable borders and gaps when only one tiled window in workspace
hl.workspace_rule({ workspace = "w[tv1]", gaps_out = 0, gaps_in = 0 })

-- Source config.d/*.lua and modes/*.lua
source_dir("config.d")
source_dir("modes")
