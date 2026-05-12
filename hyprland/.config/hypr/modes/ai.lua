-- AI Mode

local d = require("definitions")

local submap = "󰧑 AI: (O/o) focused/toggle | (I/i) ask/focus | (P/p) plan/focus | (L/l) work/focus | (s/S/c/C) speech | (w/W) copy | (q) stop | ESC"

hl.bind(("%s + O"):format(d.mod), hl.dsp.submap(submap))

local function exec_then_reset(cmd)
  return function()
    hl.exec_cmd(cmd)
    hl.dispatch(hl.dsp.submap("reset"))
  end
end

-- Speech-piped prompt → hyprpilot. `--instance NAME` auto-spawns the
-- named lane under its profile if no live instance carries that slug.
local function speech_to_pilot(args)
  return ([[zsh -c '~/.config/wayland/scripts/speech.py toggle --output stdout | hyprpilot ctl prompts send %s']]):format(
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
    hl.bind("SHIFT + o", exec_then_reset(speech_to_pilot("--draft --show")))

    -- `ask` lane — `personal/claude/opus` profile.
    hl.bind(
      "SHIFT + i",
      exec_then_reset(speech_to_pilot("--instance ask --draft --show --profile personal/claude/opus --cwd ~/notes"))
    )
    hl.bind(
      "i",
      exec_then_reset(focus_instance("--instance ask --show --ensure --profile personal/claude/opus --cwd ~/notes"))
    )

    -- `plan` lane — `kilic/kimi2.6` profile (opencode + kimi-2.6:cloud).
    hl.bind(
      "SHIFT + p",
      exec_then_reset(speech_to_pilot("--instance plan --draft --show --profile kilic/kimi2.6 --cwd ~/notes"))
    )
    hl.bind(
      "p",
      exec_then_reset(focus_instance("--instance plan --show --ensure --profile kilic/kimi2.6 --cwd ~/notes"))
    )

    -- `work` lane — `work/claude/opus` profile (laravel).
    hl.bind(
      "SHIFT + l",
      exec_then_reset(speech_to_pilot("--instance work --draft --show --profile work/claude/opus --cwd ~/notes"))
    )
    hl.bind(
      "l",
      exec_then_reset(focus_instance("--instance work --show --ensure --profile work/claude/opus --cwd ~/notes"))
    )

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

    -- Toggle the hyprpilot overlay (focused instance) — show/hide
    -- without re-prompting. Webview stays warm across hides.
    hl.bind("o", exec_then_reset("hyprpilot ctl overlay toggle"))

    -- Stop speech-to-text
    hl.bind("q", exec_then_reset(("%s kill"):format(d.speech)))

    -- Exit AI mode
    hl.bind("escape", hl.dsp.submap("reset"))
  end
)
