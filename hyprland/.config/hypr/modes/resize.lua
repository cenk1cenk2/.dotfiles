-- Resize Mode (Submap)

local d = require("definitions")

local submap = "󰕒"

hl.bind(("%s + SHIFT + R"):format(d.mod), hl.dsp.submap(submap), { description = "resize mode" })

hl.define_submap(submap, function()
  -- Resize with arrow keys and vim keys
  hl.bind(
    "left",
    hl.dsp.window.resize({ x = -24, y = 0, relative = true }),
    { repeating = true, description = "narrower" }
  )
  hl.bind(
    "down",
    hl.dsp.window.resize({ x = 0, y = 24, relative = true }),
    { repeating = true, description = "taller" }
  )
  hl.bind(
    "up",
    hl.dsp.window.resize({ x = 0, y = -24, relative = true }),
    { repeating = true, description = "shorter" }
  )
  hl.bind(
    "right",
    hl.dsp.window.resize({ x = 24, y = 0, relative = true }),
    { repeating = true, description = "wider" }
  )

  hl.bind(
    "h",
    hl.dsp.window.resize({ x = -24, y = 0, relative = true }),
    { repeating = true, description = "narrower" }
  )
  hl.bind("j", hl.dsp.window.resize({ x = 0, y = 24, relative = true }), { repeating = true, description = "taller" })
  hl.bind("k", hl.dsp.window.resize({ x = 0, y = -24, relative = true }), { repeating = true, description = "shorter" })
  hl.bind("l", hl.dsp.window.resize({ x = 24, y = 0, relative = true }), { repeating = true, description = "wider" })

  -- Larger resize with Shift
  hl.bind(
    "SHIFT + left",
    hl.dsp.window.resize({ x = -64, y = 0, relative = true }),
    { repeating = true, description = "narrower, large step" }
  )
  hl.bind(
    "SHIFT + down",
    hl.dsp.window.resize({ x = 0, y = 64, relative = true }),
    { repeating = true, description = "taller, large step" }
  )
  hl.bind(
    "SHIFT + up",
    hl.dsp.window.resize({ x = 0, y = -64, relative = true }),
    { repeating = true, description = "shorter, large step" }
  )
  hl.bind(
    "SHIFT + right",
    hl.dsp.window.resize({ x = 64, y = 0, relative = true }),
    { repeating = true, description = "wider, large step" }
  )

  hl.bind(
    "SHIFT + h",
    hl.dsp.window.resize({ x = -64, y = 0, relative = true }),
    { repeating = true, description = "narrower, large step" }
  )
  hl.bind(
    "SHIFT + j",
    hl.dsp.window.resize({ x = 0, y = 64, relative = true }),
    { repeating = true, description = "taller, large step" }
  )
  hl.bind(
    "SHIFT + k",
    hl.dsp.window.resize({ x = 0, y = -64, relative = true }),
    { repeating = true, description = "shorter, large step" }
  )
  hl.bind(
    "SHIFT + l",
    hl.dsp.window.resize({ x = -64, y = 0, relative = true }),
    { repeating = true, description = "wider, large step" }
  )

  hl.bind("escape", hl.dsp.submap("reset"), { description = "exit" })
  hl.bind("return", hl.dsp.submap("reset"), { description = "exit" })
end)
