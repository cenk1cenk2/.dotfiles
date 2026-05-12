-- Resize Mode (Submap)

local d = require("definitions")

local submap = "󰕒 Resize: ↑↓←→/hjkl | +Shift larger | ESC"

hl.bind(("%s + SHIFT + R"):format(d.mod), hl.dsp.submap(submap))

hl.define_submap(submap, function()
  -- Resize with arrow keys and vim keys
  hl.bind("left", hl.dsp.window.resize({ x = -24, y = 0, relative = true }), { repeating = true })
  hl.bind("down", hl.dsp.window.resize({ x = 0, y = 24, relative = true }), { repeating = true })
  hl.bind("up", hl.dsp.window.resize({ x = 0, y = -24, relative = true }), { repeating = true })
  hl.bind("right", hl.dsp.window.resize({ x = 24, y = 0, relative = true }), { repeating = true })

  hl.bind("h", hl.dsp.window.resize({ x = -24, y = 0, relative = true }), { repeating = true })
  hl.bind("j", hl.dsp.window.resize({ x = 0, y = 24, relative = true }), { repeating = true })
  hl.bind("k", hl.dsp.window.resize({ x = 0, y = -24, relative = true }), { repeating = true })
  hl.bind("l", hl.dsp.window.resize({ x = 24, y = 0, relative = true }), { repeating = true })

  -- Larger resize with Shift
  hl.bind("SHIFT + left", hl.dsp.window.resize({ x = -64, y = 0, relative = true }), { repeating = true })
  hl.bind("SHIFT + down", hl.dsp.window.resize({ x = 0, y = 64, relative = true }), { repeating = true })
  hl.bind("SHIFT + up", hl.dsp.window.resize({ x = 0, y = -64, relative = true }), { repeating = true })
  hl.bind("SHIFT + right", hl.dsp.window.resize({ x = 64, y = 0, relative = true }), { repeating = true })

  hl.bind("SHIFT + h", hl.dsp.window.resize({ x = -64, y = 0, relative = true }), { repeating = true })
  hl.bind("SHIFT + j", hl.dsp.window.resize({ x = 0, y = 64, relative = true }), { repeating = true })
  hl.bind("SHIFT + k", hl.dsp.window.resize({ x = 0, y = -64, relative = true }), { repeating = true })
  hl.bind("SHIFT + l", hl.dsp.window.resize({ x = -64, y = 0, relative = true }), { repeating = true })

  -- Exit resize mode
  hl.bind("escape", hl.dsp.submap("reset"))
  hl.bind("return", hl.dsp.submap("reset"))
end)
