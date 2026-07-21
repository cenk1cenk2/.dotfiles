-- Key bindings

local d = require("definitions")

-- Drag floating windows by holding down mod and left mouse button
-- Resize them with right mouse button + mod
hl.bind(("%s + mouse:272"):format(d.mod), hl.dsp.window.drag(), { mouse = true })
hl.bind(("%s + mouse:273"):format(d.mod), hl.dsp.window.resize(), { mouse = true })

-- Mouse workspace switching
hl.bind(("%s + mouse_down"):format(d.mod), hl.dsp.focus({ workspace = "e+1" }))
hl.bind(("%s + mouse_up"):format(d.mod), hl.dsp.focus({ workspace = "e-1" }))

-- Basics
hl.bind(("%s + Return"):format(d.mod), hl.dsp.exec_cmd(d.term.default))
-- Note: $term_cwd not defined in conf; preserving the binding shape.
hl.bind(("%s + SHIFT + Return"):format(d.mod), hl.dsp.exec_cmd(d.term.default))

hl.bind(("%s + SHIFT + Q"):format(d.mod), hl.dsp.window.close())
hl.bind(("%s + CTRL + SHIFT + W"):format(d.mod), hl.dsp.exec_cmd("hyprctl reload"))
hl.bind(("%s + CTRL + SHIFT + E"):format(d.mod), hl.dsp.exec_cmd(d.shutdown))

-- Launchers
hl.bind(("%s + Space"):format(d.mod), hl.dsp.exec_cmd(d.menu))
hl.bind(("%s + SHIFT + Space"):format(d.mod), hl.dsp.exec_cmd("~/.config/hypr/scripts/steal-window.py"))
hl.bind(("%s + P"):format(d.mod), hl.dsp.exec_cmd(d.clipboard))
hl.bind(("%s + SHIFT + P"):format(d.mod), hl.dsp.exec_cmd(d.quick_note))

-- Toggle Waybar
hl.bind(("%s + B"):format(d.mod), hl.dsp.exec_cmd("pkill -SIGUSR1 waybar"))

-- Media keys (locked = work on lock screen, repeating = repeatable)
hl.bind("XF86AudioRaiseVolume", d.volume.up, { repeating = true, locked = true })
hl.bind("XF86AudioLowerVolume", d.volume.down, { repeating = true, locked = true })
hl.bind("XF86AudioMute", d.volume.mute, { locked = true })
hl.bind("XF86AudioMicMute", d.volume.mic_mute, { locked = true })
hl.bind("XF86MonBrightnessUp", d.brightness.up, { repeating = true, locked = true })
hl.bind("XF86MonBrightnessDown", d.brightness.down, { repeating = true, locked = true })
hl.bind("XF86AudioPlay", d.player.toggle, { locked = true })
hl.bind("XF86AudioNext", d.player.next, { locked = true })
hl.bind("XF86AudioPrev", d.player.prev, { locked = true })
hl.bind("XF86Search", hl.dsp.exec_cmd(d.menu))
hl.bind("XF86PowerOff", hl.dsp.exec_cmd(d.shutdown))

-- Touchpad toggle. Hyprland has no device dispatcher or readable
-- per-device state (upstream #5645), so hl.device() plus tracked state
-- is the native mechanism. Hyprland device names are the lowercased,
-- dash-joined libinput names — discovered from /proc at press time so
-- any machine's touchpads match.
local touchpad_enabled = true
hl.bind("XF86TouchpadToggle", function()
  touchpad_enabled = not touchpad_enabled
  for line in io.lines("/proc/bus/input/devices") do
    local name = line:match('^N: Name="(.-)"')
    if name and name:lower():find("touchpad", 1, true) then
      hl.device({ name = (name:lower():gsub(" ", "-")), enabled = touchpad_enabled })
    end
  end
end)

-- Focus movement (vim keys and arrows)
hl.bind(("%s + h"):format(d.mod), hl.dsp.focus({ direction = "left" }))
hl.bind(("%s + j"):format(d.mod), hl.dsp.focus({ direction = "down" }))
hl.bind(("%s + k"):format(d.mod), hl.dsp.focus({ direction = "up" }))
hl.bind(("%s + l"):format(d.mod), hl.dsp.focus({ direction = "right" }))

hl.bind(("%s + left"):format(d.mod), hl.dsp.focus({ monitor = "l" }))
hl.bind(("%s + down"):format(d.mod), hl.dsp.focus({ monitor = "d" }))
hl.bind(("%s + up"):format(d.mod), hl.dsp.focus({ monitor = "u" }))
hl.bind(("%s + right"):format(d.mod), hl.dsp.focus({ monitor = "r" }))

-- Move windows within workspace
hl.bind(("%s + SHIFT + h"):format(d.mod), hl.dsp.window.move({ direction = "left", group_aware = true }))
hl.bind(("%s + SHIFT + j"):format(d.mod), hl.dsp.window.move({ direction = "down", group_aware = true }))
hl.bind(("%s + SHIFT + k"):format(d.mod), hl.dsp.window.move({ direction = "up", group_aware = true }))
hl.bind(("%s + SHIFT + l"):format(d.mod), hl.dsp.window.move({ direction = "right", group_aware = true }))

-- Move windows between monitors
hl.bind(("%s + SHIFT + left"):format(d.mod), hl.dsp.window.move({ monitor = "l" }))
hl.bind(("%s + SHIFT + down"):format(d.mod), hl.dsp.window.move({ monitor = "d" }))
hl.bind(("%s + SHIFT + up"):format(d.mod), hl.dsp.window.move({ monitor = "u" }))
hl.bind(("%s + SHIFT + right"):format(d.mod), hl.dsp.window.move({ monitor = "r" }))

-- Move workspaces between monitors
hl.bind(("%s + CTRL + SHIFT + left"):format(d.mod), hl.dsp.workspace.move({ monitor = "l" }))
hl.bind(("%s + CTRL + SHIFT + right"):format(d.mod), hl.dsp.workspace.move({ monitor = "r" }))
hl.bind(("%s + CTRL + SHIFT + up"):format(d.mod), hl.dsp.workspace.move({ monitor = "u" }))
hl.bind(("%s + CTRL + SHIFT + down"):format(d.mod), hl.dsp.workspace.move({ monitor = "d" }))

-- Alt-Tab and Super-Tab
hl.bind("ALT + Tab", hl.dsp.focus({ workspace = "previous" }))
hl.bind(("%s + Tab"):format(d.mod), hl.dsp.exec_cmd("~/.config/hypr/scripts/switch-window.py"))

-- Layout controls (matching Sway)
hl.bind(("%s + x"):format(d.mod), hl.dsp.layout("preselect l")) -- preselect horizontal split
hl.bind(("%s + v"):format(d.mod), hl.dsp.layout("preselect t")) -- preselect vertical split
hl.bind(("%s + e"):format(d.mod), hl.dsp.group.toggle()) -- toggle tabbed grouping
hl.bind(("%s + z"):format(d.mod), hl.dsp.layout("swapsplit")) -- swap split halves
hl.bind(("%s + SHIFT + z"):format(d.mod), hl.dsp.layout("togglesplit")) -- toggle split orientation

-- Workspaces
for _, ws in ipairs(d.workspaces) do
  hl.bind(("%s + %d"):format(d.mod, ws % 10), hl.dsp.focus({ workspace = ws }))
  hl.bind(("%s + SHIFT + %d"):format(d.mod, ws % 10), hl.dsp.window.move({ workspace = ws }))
end

-- Workspace navigation
hl.bind(("%s + CTRL + right"):format(d.mod), hl.dsp.focus({ workspace = "m+1" }))
hl.bind(("%s + CTRL + left"):format(d.mod), hl.dsp.focus({ workspace = "m-1" }))

-- New workspace: lowest unused ID across the session.
local function first_empty_workspace()
  local used = {}
  for _, ws in ipairs(hl.get_workspaces()) do
    used[ws.id] = true
  end
  local i = 1
  while used[i] do
    i = i + 1
  end
  return i
end

hl.bind(("%s + C"):format(d.mod), function()
  hl.dispatch(hl.dsp.focus({ workspace = first_empty_workspace() }))
end)
hl.bind(("%s + SHIFT + C"):format(d.mod), function()
  hl.dispatch(hl.dsp.window.move({ workspace = first_empty_workspace() }))
end)
hl.bind(("%s + CTRL + SHIFT + C"):format(d.mod), function()
  hl.dispatch(hl.dsp.focus({ workspace = first_empty_workspace() }))
  hl.exec_cmd(d.menu)
end)

-- Scratchpad (special workspace)
local function scratch_windows()
  local scratch = hl.get_workspace("special:scratch")
  if not scratch then
    return {}
  end

  return hl.get_workspace_windows(scratch) or {}
end

local function toggle_scratch()
  local scratch = hl.get_active_special_workspace()
  if #scratch_windows() == 0 and not (scratch and scratch.name == "special:scratch") then
    return
  end

  hl.dispatch(hl.dsp.workspace.toggle_special("scratch"))
end

local function toggle_scratch_window()
  local scratch = hl.get_active_special_workspace()
  if scratch and scratch.name == "special:scratch" and #scratch_windows() == 0 then
    toggle_scratch()
    return
  end

  local active = hl.get_active_window()
  if not active then
    return
  end
  local on_scratch = active.workspace and active.workspace.name == "special:scratch"
  if on_scratch then
    local target = hl.get_active_workspace()
    hl.dispatch(hl.dsp.window.move({ workspace = target and target.id or 1 }))
  else
    hl.dispatch(hl.dsp.window.move({ workspace = "special:scratch" }))
  end
end

hl.bind(("%s + D"):format(d.mod), toggle_scratch)
hl.bind(("%s + SHIFT + D"):format(d.mod), toggle_scratch_window)

-- Workspace swapping: move every window between the current
-- workspace and the left/right neighbour on the same monitor.
local function workspace_neighbor(direction)
  local active_ws = hl.get_active_workspace()
  local active_mon = hl.get_active_monitor()
  if not active_ws or not active_mon then
    return nil
  end
  local ids = {}
  for _, ws in ipairs(hl.get_workspaces()) do
    if ws.monitor and ws.monitor.id == active_mon.id and ws.id > 0 then
      table.insert(ids, ws.id)
    end
  end
  table.sort(ids)
  for i, id in ipairs(ids) do
    if id == active_ws.id then
      if direction == "left" then
        return ids[i - 1] or ids[#ids]
      end
      return ids[i + 1] or ids[1]
    end
  end
  return nil
end

local function swap_workspaces(direction)
  local current = hl.get_active_workspace()
  if not current then
    return
  end
  local target = workspace_neighbor(direction)
  if not target or target == current.id then
    return
  end
  local current_windows = hl.get_workspace_windows(current.id) or {}
  local target_windows = hl.get_workspace_windows(target) or {}
  for _, w in ipairs(current_windows) do
    hl.dispatch(hl.dsp.window.move({ workspace = target, window = "address:" .. w.address, follow = false }))
  end
  for _, w in ipairs(target_windows) do
    hl.dispatch(hl.dsp.window.move({ workspace = current.id, window = "address:" .. w.address, follow = false }))
  end
  hl.dispatch(hl.dsp.focus({ workspace = target }))
end

hl.bind(("%s + CTRL + SHIFT + h"):format(d.mod), function()
  swap_workspaces("left")
end)
hl.bind(("%s + CTRL + SHIFT + l"):format(d.mod), function()
  swap_workspaces("right")
end)

-- UI elements quick access
hl.bind(("%s + N"):format(d.mod), hl.dsp.exec_cmd("swaync-client -t"))
hl.bind(("%s + SHIFT + N"):format(d.mod), hl.dsp.exec_cmd(d.apps.network_manager))
hl.bind(("%s + M"):format(d.mod), hl.dsp.exec_cmd(d.apps.audio_mixer))
hl.bind(("%s + SHIFT + M"):format(d.mod), hl.dsp.exec_cmd(d.apps.bluetooth))
hl.bind(("%s + T"):format(d.mod), hl.dsp.exec_cmd(d.apps.process_manager))
hl.bind(("%s + SHIFT + T"):format(d.mod), hl.dsp.exec_cmd(d.apps.sensors))
hl.bind(("%s + G"):format(d.mod), hl.dsp.exec_cmd(d.apps.calendar))

-- Fullscreen
hl.bind(("%s + F"):format(d.mod), hl.dsp.window.fullscreen({ mode = "maximized" })) -- legacy 1
hl.bind(("%s + SHIFT + F"):format(d.mod), hl.dsp.window.fullscreen({ mode = "fullscreen" })) -- legacy 0

-- Float toggle (matches Sway mod+w)
hl.bind(("%s + W"):format(d.mod), hl.dsp.window.float({ action = "toggle" }))
hl.bind(("%s + SHIFT + W"):format(d.mod), hl.dsp.window.pin()) -- pin = sticky in Hyprland

-- Focus toggle (matches Sway mod+a = focus mode_toggle): cycle to the
-- next window of the opposite floating-ness.
hl.bind(("%s + A"):format(d.mod), function()
  local active = hl.get_active_window()
  if not active then
    hl.dispatch(hl.dsp.window.cycle_next())
    return
  end
  if active.floating then
    hl.dispatch(hl.dsp.window.cycle_next({ tiled = true }))
  else
    hl.dispatch(hl.dsp.window.cycle_next({ floating = true }))
  end
end)
hl.bind(("%s + SHIFT + A"):format(d.mod), hl.dsp.group.next()) -- cycle through group windows

-- Monitor profiles (using kanshi)
hl.bind(("%s + F1"):format(d.mod), hl.dsp.exec_cmd("jumpy display main"))
hl.bind(("%s + F2"):format(d.mod), hl.dsp.exec_cmd("jumpy display main-solo"))
hl.bind(("%s + F3"):format(d.mod), hl.dsp.exec_cmd("jumpy display main-bottom"))
hl.bind(("%s + F4"):format(d.mod), hl.dsp.exec_cmd("jumpy display main-top"))
hl.bind(("%s + F5"):format(d.mod), hl.dsp.exec_cmd("jumpy display aux"))
hl.bind(("%s + F6"):format(d.mod), hl.dsp.exec_cmd("jumpy display aux-dual"))
hl.bind(("%s + F9"):format(d.mod), hl.dsp.exec_cmd("jumpy display tv"))
hl.bind(("%s + F10"):format(d.mod), hl.dsp.exec_cmd("jumpy display tv-4k"))
hl.bind(("%s + F12"):format(d.mod), hl.dsp.exec_cmd("jumpy display reload"))

-- gestures
hl.config({
  gestures = {
    workspace_swipe_distance = 400,
    workspace_swipe_cancel_ratio = 0.5,
    workspace_swipe_create_new = false,
    workspace_swipe_forever = false,
    workspace_swipe_invert = false,
  },
})

hl.gesture({ fingers = 3, direction = "horizontal", action = "workspace" })
hl.gesture({ fingers = 3, direction = "down", action = toggle_scratch })
hl.gesture({
  fingers = 3,
  direction = "up",
  action = toggle_scratch_window,
})

-- GLOBAL SHORTCUTS
-- hyprctl globalshortcuts

-- for obsidian toggle
hl.bind("CTRL + SHIFT + O", hl.dsp.global("org.chromium.Chromium:47BF238FE34FA2C60B7414B87DA7C4AF-Ctrl+Shift+O"))
