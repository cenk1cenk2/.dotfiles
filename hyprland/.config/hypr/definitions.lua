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
M.menu = "rofi -modi 'run,drun' -show drun -run-command 'uwsm app -- {cmd}' -terminal " .. M.term.default

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
  hwatch_sensors = M.term.float_lg .. " zsh -ic 'hwatch sensors'",
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

-- On-screen display (monitor-aware). swayosd-client has no
-- focused-monitor flag, so the target is resolved per keypress —
-- in-process via hl.get_active_monitor() instead of the old
-- `$(hyprctl monitors -j | jq ...)` subshell pair. Returns a function
-- dispatcher for hl.bind.
local function osd(args)
  return function()
    local mon = hl.get_active_monitor()
    local target = mon and (" --monitor " .. mon.name) or ""
    hl.exec_cmd("swayosd-client" .. target .. " " .. args)
  end
end

-- Brightness control
local intel_backlight = io.open("/sys/class/backlight/intel_backlight", "r")
local brightness_device = intel_backlight and "--device intel_backlight " or ""

if intel_backlight then
  intel_backlight:close()
end

M.brightness = {
  up = osd(brightness_device .. "--brightness raise"),
  down = osd(brightness_device .. "--brightness lower"),
}

-- Audio control
M.volume = {
  up = osd("--output-volume raise"),
  down = osd("--output-volume lower"),
  mute = osd("--output-volume mute-toggle"),
  mic_mute = osd("--input-volume mute-toggle"),
}

-- Media player control (with OSD feedback)
M.player = {
  toggle = osd("--player spotify --playerctl play-pause"),
  next = osd("--player spotify --playerctl next"),
  prev = osd("--player spotify --playerctl previous"),
}

-- Recording
M.recorder = "~/.config/wayland/scripts/recorder.py"

-- Speech-to-text
M.speech = [[zsh -c '~/.config/wayland/scripts/speech.py "$@"' zsh]]
M.copywriter = [[zsh -c '~/.config/wayland/scripts/copywriter.py "$@"' zsh]]

return M
