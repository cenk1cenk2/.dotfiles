-- Recording Mode

local d = require("definitions")

local submap =
  "󰕧 Recording: (r/R) toggle/pause | (o) OBS | (s/S) speech→type | (c/C) speech→clip | (w/W) copywriter | (z) zoom | (q/Q) stop speech/rec | ESC"

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
  hl.bind("SHIFT + q", exec_then_reset(("%s stop"):format(d.recorder)))

  -- Speech-to-text direct typing with AI enrichment
  hl.bind("s", exec_then_reset(("%s toggle --output type --enrich"):format(d.speech)))

  -- Speech-to-text direct typing (raw, no enrichment)
  hl.bind("SHIFT + s", exec_then_reset(("%s toggle --output type"):format(d.speech)))

  -- Speech-to-text to clipboard with AI enrichment
  hl.bind("c", exec_then_reset(("%s toggle --output clipboard --enrich"):format(d.speech)))

  -- Speech-to-text to clipboard (raw, no enrichment)
  hl.bind("SHIFT + c", exec_then_reset(("%s toggle --output clipboard"):format(d.speech)))

  -- Copywriter: refine clipboard through AI
  hl.bind("w", exec_then_reset(("%s run clipboard"):format(d.copywriter)))

  -- Kill copywriter
  hl.bind("SHIFT + w", exec_then_reset(("%s kill"):format(d.copywriter)))

  -- Toggle zoom
  hl.bind("z", exec_then_reset("hypr-zoom -easing=InOutCubic -interp=Linear -target 1.5"))

  -- Stop speech-to-text
  hl.bind("q", exec_then_reset(("%s kill"):format(d.speech)))

  -- Exit recording mode
  hl.bind("escape", hl.dsp.submap("reset"))
end)
