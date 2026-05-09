-- Screenshot Mode (Submap)

local d = require("definitions")

local screenshot_name = "󰄀 Screenshot: (s) selection | (o) output | (w) window | +Shift edit | ESC"

hl.bind(d.mod .. " + S", hl.dsp.submap(screenshot_name))

local function shot_then_reset(cmd)
  return function()
    hl.exec_cmd(cmd)
    hl.dispatch(hl.dsp.submap("reset"))
  end
end

hl.define_submap(screenshot_name, function()
  -- Selection screenshot
  hl.bind("s", shot_then_reset(d.screenshot.selection.clipboard))
  hl.bind("SHIFT + s", shot_then_reset(d.screenshot.selection.edit))

  -- Output screenshot
  hl.bind("o", shot_then_reset(d.screenshot.screen.clipboard))
  hl.bind("SHIFT + o", shot_then_reset(d.screenshot.screen.edit))

  -- Window screenshot
  hl.bind("w", shot_then_reset(d.screenshot.window.clipboard))
  hl.bind("SHIFT + w", shot_then_reset(d.screenshot.window.edit))

  -- Exit screenshot mode
  hl.bind("escape", hl.dsp.submap("reset"))
end)

-- Direct screenshot with Shift+S (snipping tool)
hl.bind(d.mod .. " + SHIFT + S", hl.dsp.exec_cmd(d.screenshot.snipping_tool))
