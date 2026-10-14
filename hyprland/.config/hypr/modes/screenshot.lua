-- Screenshot Mode (Submap)

local d = require("definitions")

local submap = "󰄀"

hl.bind(("%s + S"):format(d.mod), hl.dsp.submap(submap), { description = "screenshot mode" })

local function shot_then_reset(cmd)
  return function()
    hl.exec_cmd(cmd)
    hl.dispatch(hl.dsp.submap("reset"))
  end
end

hl.define_submap(submap, function()
  -- Selection screenshot
  hl.bind("s", shot_then_reset(d.screenshot.selection.clipboard), { description = "selection to clipboard" })
  hl.bind("SHIFT + s", shot_then_reset(d.screenshot.selection.edit), { description = "selection to editor" })

  -- Output screenshot
  hl.bind("o", shot_then_reset(d.screenshot.screen.clipboard), { description = "output to clipboard" })
  hl.bind("SHIFT + o", shot_then_reset(d.screenshot.screen.edit), { description = "output to editor" })

  -- Window screenshot
  hl.bind("w", shot_then_reset(d.screenshot.window.clipboard), { description = "window to clipboard" })
  hl.bind("SHIFT + w", shot_then_reset(d.screenshot.window.edit), { description = "window to editor" })

  hl.bind("escape", hl.dsp.submap("reset"), { description = "exit" })
end)

-- Direct screenshot with Shift+S (snipping tool)
hl.bind(("%s + SHIFT + S"):format(d.mod), hl.dsp.exec_cmd(d.screenshot.snipping_tool))
