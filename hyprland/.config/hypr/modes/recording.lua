-- Recording Mode

local d = require("definitions")

local submap = "󰕧"

hl.bind(("%s + R"):format(d.mod), hl.dsp.submap(submap), { description = "recording mode" })

local function exec_then_reset(cmd)
  return function()
    hl.exec_cmd(cmd)
    hl.dispatch(hl.dsp.submap("reset"))
  end
end

hl.define_submap(submap, function()
  hl.bind("r", exec_then_reset(("%s toggle"):format(d.recorder)), { description = "toggle recording" })

  hl.bind("SHIFT + r", exec_then_reset(("%s pause"):format(d.recorder)), { description = "pause recording" })

  hl.bind("o", exec_then_reset(("%s open"):format(d.recorder)), { description = "open OBS" })

  hl.bind("SHIFT + q", exec_then_reset(("%s stop"):format(d.recorder)), { description = "stop recording" })

  hl.bind(
    "s",
    exec_then_reset(("%s stt toggle --output type --enrich"):format(d.speech)),
    { description = "dictate to typing, enriched" }
  )

  -- Speech-to-text direct typing (raw, no enrichment), through our own capture
  hl.bind(
    "SHIFT + s",
    exec_then_reset(("%s stt toggle --output type"):format(d.speech)),
    { description = "dictate to typing, raw" }
  )

  hl.bind(
    "c",
    exec_then_reset(("%s stt toggle --output clipboard --enrich"):format(d.speech)),
    { description = "dictate to clipboard, enriched" }
  )

  -- Speech-to-text to clipboard (raw, no enrichment), through our own capture
  hl.bind(
    "SHIFT + c",
    exec_then_reset(("%s stt toggle --output clipboard"):format(d.speech)),
    { description = "dictate to clipboard, raw" }
  )

  hl.bind(
    "t",
    exec_then_reset(("%s tts toggle --enrich"):format(d.speech)),
    { description = "read clipboard, enriched" }
  )

  hl.bind("SHIFT + t", exec_then_reset(("%s tts toggle"):format(d.speech)), { description = "read clipboard, raw" })

  hl.bind(
    "g",
    exec_then_reset(("%s tts toggle --enrich --style summary"):format(d.speech)),
    { description = "read clipboard summary" }
  )

  -- Pause/resume the dictation. Resets like the stt toggles: live typing
  -- sends keystrokes through the compositor, and an open submap would catch them.
  hl.bind("d", exec_then_reset(("%s stt pause"):format(d.speech)), { description = "pause dictation" })

  hl.bind("f", hl.dsp.exec_cmd(("%s tts pause"):format(d.speech)), { repeating = true, description = "pause reading" })

  hl.bind("SHIFT + f", exec_then_reset(("%s tts kill"):format(d.speech)), { description = "stop reading" })

  -- Scrub the utterance. Deliberately without the reset every other bind
  -- here carries: scrubbing is held down, and leaving the submap after the
  -- first step would cost a Super+R for every three seconds.
  hl.bind(
    "right",
    hl.dsp.exec_cmd(("%s tts seek 3"):format(d.speech)),
    { repeating = true, description = "scrub forward 3s" }
  )
  hl.bind(
    "left",
    hl.dsp.exec_cmd(("%s tts seek -3"):format(d.speech)),
    { repeating = true, description = "scrub back 3s" }
  )

  hl.bind("SHIFT + v", exec_then_reset([[wtype "/copy" && wtype -k Return]]), { description = "copy agent reply" })

  -- Paste into the focused window. Ctrl+Shift+V rather than Ctrl+V, which is
  -- what pastes in kitty.
  hl.bind(
    "v",
    exec_then_reset(
      [[a=$(hyprctl -j activewindow | jq -r .address) && ~/.config/hypr/scripts/send-key.py "address:$a" V -m CTRL+SHIFT]]
    ),
    { description = "paste" }
  )

  hl.bind("a", exec_then_reset(d.agents), { description = "pick agent, waiting first" })

  hl.bind("w", exec_then_reset(("%s run clipboard"):format(d.copywriter)), { description = "copywriter on clipboard" })

  hl.bind("SHIFT + w", exec_then_reset(("%s kill"):format(d.copywriter)), { description = "kill copywriter" })

  hl.bind("z", exec_then_reset(("%s toggle"):format(d.zoom)), { description = "toggle zoom" })

  hl.bind("q", exec_then_reset(("%s stt kill"):format(d.speech)), { description = "stop dictation" })

  hl.bind("escape", hl.dsp.submap("reset"), { description = "exit" })
end)
