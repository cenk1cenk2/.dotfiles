-- Recording Mode

local d = require("definitions")

local submap = "󰕧 Recording: (r/R) toggle/pause | (o) OBS | (z) zoom | (q) stop | ESC"

hl.bind(("%s + R"):format(d.mod), hl.dsp.submap(submap))

local function exec_then_reset(cmd)
  return function()
    hl.exec_cmd(cmd)
    hl.dispatch(hl.dsp.submap("reset"))
  end
end

hl.define_submap(submap, function()
  -- Toggle recording (start/stop)
  hl.bind("r", exec_then_reset(("%s toggle"):format(d.recorder)))

  -- Pause/resume recording
  hl.bind("SHIFT + r", exec_then_reset(("%s pause"):format(d.recorder)))

  -- Open OBS window
  hl.bind("o", exec_then_reset(("%s open"):format(d.recorder)))

  -- Stop recording
  hl.bind("q", exec_then_reset(("%s stop"):format(d.recorder)))

  -- Toggle zoom
  hl.bind("z", exec_then_reset("hypr-zoom -easing=InOutCubic -interp=Linear -target 1.5"))

  -- Exit recording mode
  hl.bind("escape", hl.dsp.submap("reset"))
end)
