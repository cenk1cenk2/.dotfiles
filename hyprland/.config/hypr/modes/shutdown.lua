-- Shutdown Mode (Submap)
-- Triggered by d.shutdown variable (see definitions.lua)
-- Keybindings match wlogout configuration

local d = require("definitions")

local submap = "󰐥 Power: (l) lock | (s) suspend | (S) hibernate | (L) logout | (R) reboot | (Q) shutdown | ESC"

local function exec_then_reset(cmd)
  return function()
    hl.exec_cmd(cmd)
    hl.dispatch(hl.dsp.submap("reset"))
  end
end

hl.define_submap(submap, function()
  -- Lock
  hl.bind("l", exec_then_reset(d.locking))

  -- Hibernate
  hl.bind("SHIFT + s", exec_then_reset("systemctl hibernate"))

  -- Logout
  hl.bind("SHIFT + l", exec_then_reset("loginctl terminate-user $USER"))

  -- Shutdown
  hl.bind("SHIFT + q", exec_then_reset("systemctl poweroff"))

  -- Suspend
  hl.bind("s", exec_then_reset("systemctl suspend"))

  -- Reboot
  hl.bind("SHIFT + r", exec_then_reset("systemctl reboot"))

  -- Exit shutdown mode
  hl.bind("escape", hl.dsp.submap("reset"))
end)
