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
M.screenshot = {}
M.screenshot.grimshot = "~/.config/hypr/scripts/grimshot.sh"
M.screenshot.screen = {
  clipboard = M.screenshot.grimshot .. " --notify copy output",
  file = M.screenshot.grimshot .. " --notify save output",
  edit = M.screenshot.grimshot .. " --notify save output - | swappy -f -",
}
M.screenshot.selection = {
  clipboard = M.screenshot.grimshot .. " --notify copy area",
  file = M.screenshot.grimshot .. " --notify save area",
  edit = M.screenshot.grimshot .. " --notify save area - | swappy -f -",
}
M.screenshot.window = {
  clipboard = M.screenshot.grimshot .. " --notify copy active",
  file = M.screenshot.grimshot .. " --notify save active",
  edit = M.screenshot.grimshot .. " --notify save active - | swappy -f -",
}
M.screenshot.snipping_tool = M.screenshot.grimshot .. " --notify save area - | swappy -f -"

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

return M
