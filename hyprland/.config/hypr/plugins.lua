-- Hyprland Plugins

-- Auto-install and load plugins at startup
-- hl.on("hyprland.start", function() hl.exec_cmd("~/.config/hypr/scripts/install-plugins.sh") end)
-- hl.on("hyprland.start", function() hl.exec_cmd("hyprpm reload -n") end)

-- hy3 - i3/sway-like tiling layout (DISABLED - using native dwindle layout)
-- https://github.com/outfoxxed/hy3
--
--[[
hl.config({
  plugin = {
    hy3 = {
      no_gaps_when_only = 1,
      node_collapse_policy = 2,

      tabs = {
        height = 24,
        padding = 2,
        from_top = false,
        radius = 0,
        render_text = true,
        text_center = true,
        text_font = theme.font.gui,
        text_height = 12,
        text_padding = 4,

        col = {
          active = theme.colors[3],
          ["active.border"] = theme.colors[11],
          ["active.text"] = theme.colors[0],
          urgent = theme.colors[1],
          ["urgent.text"] = theme.colors[0],
          inactive = theme.colors[8],
          ["inactive.text"] = theme.colors[0],
        },
      },

      autotile = {
        enable = true,
        ephemeral_groups = true,
        trigger_width = 800,
        trigger_height = 600,
        workspaces = "all",
      },
    },
  },
})

hl.config({ general = { layout = "hy3" } })

-- hy3 keybinding overrides
-- Uncomment below (along with plugin block above) to re-enable hy3.
-- These unbind the native dwindle keys and rebind them with hy3 dispatchers.

-- Focus movement
hl.unbind(d.mod .. " + h")
hl.unbind(d.mod .. " + j")
hl.unbind(d.mod .. " + k")
hl.unbind(d.mod .. " + l")
hl.bind(d.mod .. " + h", hl.dispatch("hy3:movefocus", "l"))
hl.bind(d.mod .. " + j", hl.dispatch("hy3:movefocus", "d"))
hl.bind(d.mod .. " + k", hl.dispatch("hy3:movefocus", "u"))
hl.bind(d.mod .. " + l", hl.dispatch("hy3:movefocus", "r"))

-- Move windows within workspace
hl.unbind(d.mod .. " + SHIFT + h")
hl.unbind(d.mod .. " + SHIFT + j")
hl.unbind(d.mod .. " + SHIFT + k")
hl.unbind(d.mod .. " + SHIFT + l")
hl.bind(d.mod .. " + SHIFT + h", hl.dispatch("hy3:movewindow", "l"))
hl.bind(d.mod .. " + SHIFT + j", hl.dispatch("hy3:movewindow", "d"))
hl.bind(d.mod .. " + SHIFT + k", hl.dispatch("hy3:movewindow", "u"))
hl.bind(d.mod .. " + SHIFT + l", hl.dispatch("hy3:movewindow", "r"))

-- Layout controls
hl.unbind(d.mod .. " + x")
hl.unbind(d.mod .. " + v")
hl.unbind(d.mod .. " + e")
hl.unbind(d.mod .. " + z")
hl.unbind(d.mod .. " + SHIFT + z")
hl.bind(d.mod .. " + x", hl.dispatch("hy3:makegroup", "h", "ephemeral"))
hl.bind(d.mod .. " + v", hl.dispatch("hy3:makegroup", "v", "ephemeral"))
hl.bind(d.mod .. " + e", hl.dispatch("hy3:changegroup", "toggletab"))
hl.bind(d.mod .. " + z", hl.dispatch("hy3:expand", "expand"))
hl.bind(d.mod .. " + SHIFT + z", hl.dispatch("hy3:expand", "shrink"))

-- Focus parent
hl.unbind(d.mod .. " + SHIFT + A")
hl.bind(d.mod .. " + SHIFT + A", hl.dispatch("hy3:changefocus", "raise_or_top"))
--]]
