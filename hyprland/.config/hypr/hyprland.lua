-- Hyprland configuration (Lua, 0.55+)
-- Modular structure inspired by Sway configuration.
-- Renamed from hyprland.lua so the legacy hyprland.conf keeps loading
-- on 0.54. To activate: rename this to `hyprland.lua`.

local config_dir = ("%s/.config/hypr"):format(os.getenv("HOME"))

package.path = table.concat({
  ("%s/?.lua"):format(config_dir),
  ("%s/?/init.lua"):format(config_dir),
  ("%s/config.d/?.lua"):format(config_dir),
  package.path,
}, ";")

table.insert(package.searchers, function(name)
  local path = package.searchpath(name, package.path, ".", ".")
  if path then
    return loadfile(path)
  end
end)

-- Environment variables.
--
-- The 3rd `true` arg propagates the var to systemd + dbus via
-- `systemctl --user import-environment` + `dbus-update-activation-environment`
-- so services spawned by user@.service (XDG autostart, polkit, etc.)
-- inherit them. Use it for any var that systemd-spawned Qt/GTK/Electron
-- apps need to see.
--
-- Hyprland 0.55 already auto-imports DISPLAY, WAYLAND_DISPLAY,
-- HYPRLAND_INSTANCE_SIGNATURE, XDG_CURRENT_DESKTOP, QT_QPA_PLATFORMTHEME,
-- PATH, XDG_DATA_DIRS in startCompositor — no `, true` needed for those.
--
-- Gaming, Firefox and WLR vars stay process-local: they only matter to
-- children Hyprland spawns directly, not to systemd-managed units.

hl.env("XDG_SESSION_TYPE", "wayland", true)
hl.env("XDG_CURRENT_DESKTOP", "Hyprland")

-- Libseat backend (Hyprland-internal, no propagation needed)
hl.env("LIBSEAT_BACKEND", "logind")

-- WLR settings (Hyprland's own XWayland process inherits this directly)
-- hl.env("WLR_RENDERER_ALLOW_SOFTWARE", "1")
hl.env("WLR_XWAYLAND", "/usr/local/bin/Xwayland")

-- Qt settings — propagate so Qt apps under systemd render via Wayland
hl.env("QT_QPA_PLATFORM", "wayland", true)
hl.env("QT_QPA_PLATFORMTHEME", "gtk3")
hl.env("QT_WAYLAND_DISABLE_WINDOWDECORATION", "1", true)
hl.env("QT_QUICK_CONTROLS_STYLE", "org.hyprland.style", true)

-- GTK / Electron — propagate so GTK/Electron apps under systemd use
-- Wayland and the portal interfaces
hl.env("GTK_USE_PORTAL", "1", true)
hl.env("GDK_BACKEND", "wayland", true)
hl.env("GDK_DEBUG", "portals")
hl.env("ELECTRON_OZONE_PLATFORM_HINT", "wayland", true)

-- Gaming optimizations (Hyprland-spawned children only)
hl.env("PROTON_DXVK_LOWLATENCY", "1")
hl.env("MANGOHUD", "1")
hl.env("VKD3D_CONFIG", "dxr,dxr11")
hl.env("VKD3D_FEATURE_LEVEL", "12_2")
hl.env("PROTON_ENABLE_WAYLAND", "1")
hl.env("DXVK_HDR", "1")
hl.env("PROTON_ENABLE_HDR", "1")

-- Firefox/Mozilla (Hyprland-spawned only)
hl.env("MOZ_DBUS_REMOTE", "1")

-- Add mise shims and user bin to PATH
hl.env(
  "PATH",
  ("%s/.local/share/mise/shims:%s/.local/bin:%s"):format(os.getenv("HOME"), os.getenv("HOME"), os.getenv("PATH") or "")
)

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

-- Source config.d/ and modes/ (each via its init.lua re-exports).
require("config.d")
require("modes")
