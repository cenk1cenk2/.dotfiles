-- AI Mode

local d = require("definitions")

local submap =
  "󰧑 AI: (O/o) focused/toggle | (I/i) ask/focus | (P/p) plan/focus | (L/l) work/focus | (s/S/c/C) speech | (w/W) copy | (q) stop | ESC"

hl.bind(("%s + O"):format(d.mod), hl.dsp.submap(submap))

-- Speech-piped prompt → hyprpilot. `--enrich` runs the transcript
-- through the configured LLM enricher before stdout, so the pilot
-- composer gets cleaned-up text instead of raw speech. `--instance
-- NAME` auto-spawns the named lane under its profile if no live
-- instance carries that slug.
local function speech_to_pilot(args)
  return ([[zsh -c '~/.config/wayland/scripts/speech.py toggle --output stdout --enrich | hyprpilot ctl prompts send %s']]):format(
    args
  )
end

-- Focus + present an existing hyprpilot instance — same shape every time.
local function focus_instance(args)
  return ("hyprpilot ctl instances focus %s"):format(args)
end

hl.define_submap(submap, function()
  -- Talk to whatever hyprpilot instance is currently focused.
  -- Omitting `--instance` makes hyprpilot fall back to the focused
  -- pointer; if none is live the daemon errors out. Use `I`/`P`/`L`
  -- to spawn the named lanes.
  hl.bind("SHIFT + o", function()
    hl.exec_cmd(speech_to_pilot("--draft --show"))
    hl.dispatch(hl.dsp.submap("reset"))
  end)

  -- `plan` lane — `kilic/kimi2.6` profile (opencode + kimi-2.6:cloud).
  hl.bind("SHIFT + p", function()
    hl.exec_cmd(speech_to_pilot("--instance plan --draft --show --profile personal/kilic/minimax-m3 --cwd ~/notes"))
    hl.dispatch(hl.dsp.submap("reset"))
  end)
  hl.bind("p", function()
    hl.exec_cmd(focus_instance("--instance plan --show --ensure --profile personal/kilic/minimax-m3 --cwd ~/notes"))
    hl.dispatch(hl.dsp.submap("reset"))
  end)

  -- Speech-to-text direct typing with AI enrichment
  hl.bind("s", function()
    hl.exec_cmd(("%s toggle --output type --enrich"):format(d.speech))
    hl.dispatch(hl.dsp.submap("reset"))
  end)

  -- Speech-to-text direct typing (raw, no enrichment)
  hl.bind("SHIFT + s", function()
    hl.exec_cmd(("%s toggle --output type"):format(d.speech))
    hl.dispatch(hl.dsp.submap("reset"))
  end)

  -- Speech-to-text to clipboard with AI enrichment
  hl.bind("c", function()
    hl.exec_cmd(("%s toggle --output clipboard --enrich"):format(d.speech))
    hl.dispatch(hl.dsp.submap("reset"))
  end)

  -- Speech-to-text to clipboard (raw, no enrichment)
  hl.bind("SHIFT + c", function()
    hl.exec_cmd(("%s toggle --output clipboard"):format(d.speech))
    hl.dispatch(hl.dsp.submap("reset"))
  end)

  -- Copywriter: refine clipboard through AI
  hl.bind("w", function()
    hl.exec_cmd(("%s run clipboard"):format(d.copywriter))
    hl.dispatch(hl.dsp.submap("reset"))
  end)

  -- Kill copywriter
  hl.bind("SHIFT + w", function()
    hl.exec_cmd(("%s kill"):format(d.copywriter))
    hl.dispatch(hl.dsp.submap("reset"))
  end)

  -- Toggle the hyprpilot overlay (focused instance) — show/hide
  -- without re-prompting. Webview stays warm across hides.
  hl.bind("o", function()
    hl.exec_cmd("hyprpilot ctl overlay toggle")
    hl.dispatch(hl.dsp.submap("reset"))
  end)

  -- Stop speech-to-text
  hl.bind("q", function()
    hl.exec_cmd(("%s kill"):format(d.speech))
    hl.dispatch(hl.dsp.submap("reset"))
  end)

  -- Exit AI mode
  hl.bind("escape", hl.dsp.submap("reset"))
end)
