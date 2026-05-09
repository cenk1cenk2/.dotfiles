-- Recording Mode

local d = require("definitions")

hl.bind(d.mod .. " + R", hl.dsp.submap("󰕧 Recording: (r/R) toggle/pause | (o) OBS | (z) zoom | (q) stop | ESC"))

local function exec_then_reset(cmd)
  return function()
    hl.exec_cmd(cmd)
    hl.dispatch(hl.dsp.submap("reset"))
  end
end

hl.define_submap("󰕧 Recording: (r/R) toggle/pause | (o) OBS | (z) zoom | (q) stop | ESC", function()
  -- Toggle recording (start/stop)
  hl.bind("r", exec_then_reset(d.recorder .. " toggle"))

  -- Pause/resume recording
  hl.bind("SHIFT + r", exec_then_reset(d.recorder .. " pause"))

  -- Open OBS window
  hl.bind("o", exec_then_reset(d.recorder .. " open"))

  -- Stop recording
  hl.bind("q", exec_then_reset(d.recorder .. " stop"))

  -- Toggle zoom
  hl.bind("z", exec_then_reset("hypr-zoom -easing=InOutCubic -interp=Linear -target 1.5"))

  -- Exit recording mode
  hl.bind("escape", hl.dsp.submap("reset"))
end)
