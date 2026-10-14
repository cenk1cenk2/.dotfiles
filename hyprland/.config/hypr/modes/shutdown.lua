-- Shutdown Mode (Submap)
-- Triggered by d.shutdown variable (see definitions.lua)
-- Keybindings match wlogout configuration

local d = require("definitions")

local submap = "󰐥"

local function exec_then_reset(cmd)
  return function()
    hl.exec_cmd(cmd)
    hl.dispatch(hl.dsp.submap("reset"))
  end
end

hl.define_submap(submap, function()
  hl.bind("l", exec_then_reset(d.locking), { description = "lock" })
  hl.bind("SHIFT + s", exec_then_reset("systemctl hibernate"), { description = "hibernate" })
  hl.bind("SHIFT + l", exec_then_reset("loginctl terminate-user $USER"), { description = "logout" })
  hl.bind("SHIFT + q", exec_then_reset("systemctl poweroff"), { description = "shut down" })
  hl.bind("s", exec_then_reset("systemctl suspend"), { description = "suspend" })
  hl.bind("SHIFT + r", exec_then_reset("systemctl reboot"), { description = "reboot" })
  hl.bind("escape", hl.dsp.submap("reset"), { description = "exit" })
end)
