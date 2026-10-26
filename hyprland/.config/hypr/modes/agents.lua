-- Agents Mode (Submap)

local d = require("definitions")

local submap = "󰚩"

hl.bind(("%s + A"):format(d.mod), hl.dsp.submap(submap), { description = "agents mode" })
hl.bind(("%s + SHIFT + A"):format(d.mod), hl.dsp.exec_cmd(("%s pick"):format(d.agents)), { description = "pick agent" })

local function exec_then_reset(cmd)
  return function()
    hl.exec_cmd(cmd)
    hl.dispatch(hl.dsp.submap("reset"))
  end
end

hl.define_submap(submap, function()
  -- Stepping stays in the submap, so a row of agents is walked one key at a time.
  hl.bind("l", hl.dsp.exec_cmd(("%s next"):format(d.agents)), { description = "next agent" })
  hl.bind("right", hl.dsp.exec_cmd(("%s next"):format(d.agents)), { description = "next agent" })
  hl.bind("h", hl.dsp.exec_cmd(("%s prev"):format(d.agents)), { description = "previous agent" })
  hl.bind("left", hl.dsp.exec_cmd(("%s prev"):format(d.agents)), { description = "previous agent" })

  hl.bind("w", exec_then_reset(("%s waiting"):format(d.agents)), { description = "oldest waiting agent" })
  hl.bind(
    "SHIFT + w",
    exec_then_reset(("%s waiting --newest"):format(d.agents)),
    { description = "newest waiting agent" }
  )
  hl.bind("d", exec_then_reset(("%s dismiss"):format(d.agents)), { description = "clear waiting list" })

  hl.bind("space", exec_then_reset(("%s pick"):format(d.agents)), { description = "pick agent" })
  hl.bind("a", exec_then_reset(("%s pick"):format(d.agents)), { description = "pick agent" })

  -- Dictation pastes into the focused window, and the submap would catch
  -- the paste key, so it leaves the submap like the recording mode's stt binds.
  hl.bind(
    "s",
    exec_then_reset(("%s stt toggle --output paste --enrich --submit"):format(d.speech)),
    { description = "dictate" }
  )

  -- Paste into the focused window. Ctrl+Shift+V rather than Ctrl+V, which is
  -- what pastes in kitty.
  hl.bind(
    "v",
    exec_then_reset(
      [[a=$(hyprctl -j activewindow | jq -r .address) && ~/.config/hypr/scripts/send-key.py "address:$a" V -m CTRL+SHIFT]]
    ),
    { description = "paste" }
  )
  hl.bind("c", exec_then_reset([[wtype "/copy" && wtype -k Return]]), { description = "copy agent reply" })

  hl.bind("escape", hl.dsp.submap("reset"), { description = "exit" })
end)
