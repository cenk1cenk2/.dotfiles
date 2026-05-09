-- Variables shared across all hyprland-new.lua modules.
-- Mirror of definitions.conf with semantic grouping.

local M = {}

-- Logo key. Use Mod1 for Alt and Mod4 for Super.
M.mod = "SUPER"

-- Terminal emulators
M.term = {
  default = "kitty",
  float = "kitty --class floating_shell",
  float_lg = "kitty --class floating_shell_lg",
  float_portrait = "kitty --class floating_shell_portrait",
  float_portrait_lg = "kitty --class floating_shell_portrait_lg",
}

-- Application launcher
M.menu = "rofi -modi 'run,drun' -show drun -terminal " .. M.term.default

-- Clipboard manager (clipse)
M.clipboard = M.term.float .. " --class clipse clipse"

-- Quick note -> clipboard (nvim scratch; on :w pipes buffer to cbcp)
M.quick_note = M.term.float .. " zsh -c '~/.config/wayland/scripts/quick-note.sh'"

-- Lockscreen configuration
M.locking = "hyprlock"

-- Shutdown mode
M.shutdown = [==[bash -c '[[ "$(pgrep -x wlogout)" ]] && pkill wlogout || wlogout']==]

-- Utility applications
M.apps = {
  network_manager = M.term.float .. " nmtui",
  bluetooth = M.term.float .. " bluetuith",
  audio_mixer = M.term.float_portrait .. " wiremix",
  calendar = M.term.float .. " ikhal",
  process_manager = M.term.float_portrait_lg .. " btop",
  sensors = M.term.float_lg .. " zsh -ic 'hwatch sensors'",
}

-- Volume query commands (return current %, no UI)
M.volume_query = {
  sink = "pactl get-sink-volume @DEFAULT_SINK@ | grep '^Volume:' | cut -d / -f 2 | tr -d ' ' | sed 's/%//'",
  source = "pactl get-source-volume @DEFAULT_SOURCE@ | grep '^Volume:' | cut -d / -f 2 | tr -d ' ' | sed 's/%//'",
}

-- Workspace IDs
M.workspaces = { 1, 2, 3, 4, 5, 6, 7, 8, 9, 10 }

-- Screenshot tools
local grimshot = "~/.config/hypr/scripts/grimshot.sh"
M.screenshot = {
  grimshot = grimshot,
  screen = {
    clipboard = grimshot .. " --notify copy output",
    file = grimshot .. " --notify save output",
    edit = grimshot .. " --notify save output - | swappy -f -",
  },
  selection = {
    clipboard = grimshot .. " --notify copy area",
    file = grimshot .. " --notify save area",
    edit = grimshot .. " --notify save area - | swappy -f -",
  },
  window = {
    clipboard = grimshot .. " --notify copy active",
    file = grimshot .. " --notify save active",
    edit = grimshot .. " --notify save active - | swappy -f -",
  },
  snipping_tool = grimshot .. " --notify save area - | swappy -f -",
}

-- On-screen display (monitor-aware)
M.osd = [[swayosd-client --monitor $(hyprctl monitors -j | jq -r '.[] | select(.focused) | .name')]]

-- Brightness control
M.brightness = {
  up = M.osd .. " --brightness raise",
  down = M.osd .. " --brightness lower",
}

-- Audio control
M.volume = {
  up = M.osd .. " --output-volume raise",
  down = M.osd .. " --output-volume lower",
  mute = M.osd .. " --output-volume mute-toggle",
  mic_mute = M.osd .. " --input-volume mute-toggle",
}

-- Media player control (with OSD feedback)
M.player = {
  toggle = M.osd .. " --player spotify --playerctl play-pause",
  next = M.osd .. " --player spotify --playerctl next",
  prev = M.osd .. " --player spotify --playerctl previous",
}

-- Recording
M.recorder = "~/.config/wayland/scripts/recorder.py"

-- Speech-to-text
M.speech = [[zsh -c '~/.config/wayland/scripts/speech.py "$@"' zsh]]
M.copywriter = [[zsh -c '~/.config/wayland/scripts/copywriter.py "$@"' zsh]]

-- hyprpilot — ACP overlay (replaces the legacy pilot.py). Keeping
-- `pilot` as the public alias so existing bindings keep working;
-- the value just dispatches the new `ctl` surface.
M.pilot = "/usr/local/bin/hyprpilot ctl"

-- Tablet mapping
M.tablet_map_to_output = [[hyprctl keyword input:tablet:output "$(hyprctl monitors -j | jq -r '.[] | select(.focused) | .name')"]]

return M
