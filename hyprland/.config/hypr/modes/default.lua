-- Key bindings

local d = require("definitions")

-- Drag floating windows by holding down mod and left mouse button
-- Resize them with right mouse button + mod
hl.bind(d.mod .. " + mouse:272", hl.dsp.window.drag(), { mouse = true })
hl.bind(d.mod .. " + mouse:273", hl.dsp.window.resize(), { mouse = true })

-- Mouse workspace switching
hl.bind(d.mod .. " + mouse_down", hl.dsp.focus({ workspace = "e+1" }))
hl.bind(d.mod .. " + mouse_up", hl.dsp.focus({ workspace = "e-1" }))

-- Basics
hl.bind(d.mod .. " + Return", hl.dsp.exec_cmd(d.term.default))
-- Note: $term_cwd not defined in conf; preserving the binding shape.
hl.bind(d.mod .. " + SHIFT + Return", hl.dsp.exec_cmd(d.term.default))

hl.bind(d.mod .. " + SHIFT + Q", hl.dsp.window.close())
hl.bind(d.mod .. " + CTRL + SHIFT + W", hl.dsp.exec_cmd("hyprctl reload"))
hl.bind(d.mod .. " + CTRL + SHIFT + E", hl.dsp.exec_cmd(d.shutdown))

-- Launchers
hl.bind(d.mod .. " + Space", hl.dsp.exec_cmd(d.menu))
hl.bind(d.mod .. " + SHIFT + Space", hl.dsp.exec_cmd("~/.config/hypr/scripts/steal-window.py"))
hl.bind(d.mod .. " + P", hl.dsp.exec_cmd(d.clipboard))
hl.bind(d.mod .. " + SHIFT + P", hl.dsp.exec_cmd(d.quick_note))

-- Toggle Waybar
hl.bind(d.mod .. " + B", hl.dsp.exec_cmd("pkill -SIGUSR1 waybar"))

-- Media keys (locked = work on lock screen, repeating = repeatable)
hl.bind("XF86AudioRaiseVolume", hl.dsp.exec_cmd(d.volume.up), { repeating = true, locked = true })
hl.bind("XF86AudioLowerVolume", hl.dsp.exec_cmd(d.volume.down), { repeating = true, locked = true })
hl.bind("XF86AudioMute", hl.dsp.exec_cmd(d.volume.mute), { locked = true })
hl.bind("XF86AudioMicMute", hl.dsp.exec_cmd(d.volume.mic_mute), { locked = true })
hl.bind("XF86MonBrightnessUp", hl.dsp.exec_cmd(d.brightness.up), { repeating = true, locked = true })
hl.bind("XF86MonBrightnessDown", hl.dsp.exec_cmd(d.brightness.down), { repeating = true, locked = true })
hl.bind("XF86AudioPlay", hl.dsp.exec_cmd(d.player.toggle), { locked = true })
hl.bind("XF86AudioNext", hl.dsp.exec_cmd(d.player.next), { locked = true })
hl.bind("XF86AudioPrev", hl.dsp.exec_cmd(d.player.prev), { locked = true })
hl.bind("XF86Search", hl.dsp.exec_cmd(d.menu))
hl.bind("XF86PowerOff", hl.dsp.exec_cmd(d.shutdown))

-- Touchpad toggle
hl.bind("XF86TouchpadToggle", hl.dsp.exec_cmd("hyprctl keyword 'device[touchpad]:enabled' toggle"))

-- Focus movement (vim keys and arrows)
hl.bind(d.mod .. " + h", hl.dsp.focus({ direction = "left" }))
hl.bind(d.mod .. " + j", hl.dsp.focus({ direction = "down" }))
hl.bind(d.mod .. " + k", hl.dsp.focus({ direction = "up" }))
hl.bind(d.mod .. " + l", hl.dsp.focus({ direction = "right" }))

hl.bind(d.mod .. " + left", hl.dsp.focus({ monitor = "l" }))
hl.bind(d.mod .. " + down", hl.dsp.focus({ monitor = "d" }))
hl.bind(d.mod .. " + up", hl.dsp.focus({ monitor = "u" }))
hl.bind(d.mod .. " + right", hl.dsp.focus({ monitor = "r" }))

-- Move windows within workspace
hl.bind(d.mod .. " + SHIFT + h", hl.dsp.window.move({ direction = "left", group_aware = true }))
hl.bind(d.mod .. " + SHIFT + j", hl.dsp.window.move({ direction = "down", group_aware = true }))
hl.bind(d.mod .. " + SHIFT + k", hl.dsp.window.move({ direction = "up", group_aware = true }))
hl.bind(d.mod .. " + SHIFT + l", hl.dsp.window.move({ direction = "right", group_aware = true }))

-- Move windows between monitors
hl.bind(d.mod .. " + SHIFT + left", hl.dsp.window.move({ monitor = "l" }))
hl.bind(d.mod .. " + SHIFT + down", hl.dsp.window.move({ monitor = "d" }))
hl.bind(d.mod .. " + SHIFT + up", hl.dsp.window.move({ monitor = "u" }))
hl.bind(d.mod .. " + SHIFT + right", hl.dsp.window.move({ monitor = "r" }))

-- Move workspaces between monitors
hl.bind(d.mod .. " + CTRL + SHIFT + left", hl.dsp.workspace.move({ monitor = "l" }))
hl.bind(d.mod .. " + CTRL + SHIFT + right", hl.dsp.workspace.move({ monitor = "r" }))
hl.bind(d.mod .. " + CTRL + SHIFT + up", hl.dsp.workspace.move({ monitor = "u" }))
hl.bind(d.mod .. " + CTRL + SHIFT + down", hl.dsp.workspace.move({ monitor = "d" }))

-- Alt-Tab and Super-Tab
hl.bind("ALT + Tab", hl.dsp.focus({ workspace = "previous" }))
hl.bind(d.mod .. " + Tab", hl.dsp.exec_cmd("~/.config/hypr/scripts/switch-window.py"))

-- Layout controls (matching Sway)
hl.bind(d.mod .. " + x", hl.dsp.layout("preselect l")) -- preselect horizontal split
hl.bind(d.mod .. " + v", hl.dsp.layout("preselect t")) -- preselect vertical split
hl.bind(d.mod .. " + e", hl.dsp.group.toggle()) -- toggle tabbed grouping
hl.bind(d.mod .. " + z", hl.dsp.layout("swapsplit")) -- swap split halves
hl.bind(d.mod .. " + SHIFT + z", hl.dsp.layout("togglesplit")) -- toggle split orientation

-- Workspaces
for _, ws in ipairs(d.workspaces) do
  hl.bind(d.mod .. " + " .. tostring(ws % 10), hl.dsp.focus({ workspace = ws }))
  hl.bind(d.mod .. " + SHIFT + " .. tostring(ws % 10), hl.dsp.window.move({ workspace = ws }))
end

-- Workspace navigation
hl.bind(d.mod .. " + CTRL + right", hl.dsp.focus({ workspace = "m+1" }))
hl.bind(d.mod .. " + CTRL + left", hl.dsp.focus({ workspace = "m-1" }))

-- New workspace (uses custom script to find empty workspace on current monitor)
hl.bind(d.mod .. " + C", hl.dsp.exec_cmd("~/.config/hypr/scripts/new-workspace.py --switch"))
hl.bind(d.mod .. " + SHIFT + C", hl.dsp.exec_cmd("~/.config/hypr/scripts/new-workspace.py --move"))
hl.bind(d.mod .. " + CTRL + SHIFT + C", hl.dsp.exec_cmd("~/.config/hypr/scripts/new-workspace.py --switch"))
hl.bind(d.mod .. " + CTRL + SHIFT + C", hl.dsp.exec_cmd(d.menu))

-- Scratchpad (special workspace)
hl.bind(d.mod .. " + D", hl.dsp.workspace.toggle_special("scratch"))
hl.bind(d.mod .. " + SHIFT + D", hl.dsp.exec_cmd("~/.config/hypr/scripts/scratchpad-toggle.py"))

-- Workspace swapping
hl.bind(d.mod .. " + CTRL + SHIFT + h", hl.dsp.exec_cmd("~/.config/hypr/scripts/swap-workspace.py -s left"))
hl.bind(d.mod .. " + CTRL + SHIFT + l", hl.dsp.exec_cmd("~/.config/hypr/scripts/swap-workspace.py -s right"))

-- UI elements quick access
hl.bind(d.mod .. " + N", hl.dsp.exec_cmd("swaync-client -t"))
hl.bind(d.mod .. " + SHIFT + N", hl.dsp.exec_cmd(d.apps.network_manager))
hl.bind(d.mod .. " + M", hl.dsp.exec_cmd(d.apps.audio_mixer))
hl.bind(d.mod .. " + SHIFT + M", hl.dsp.exec_cmd(d.apps.bluetooth))
hl.bind(d.mod .. " + T", hl.dsp.exec_cmd(d.apps.process_manager))
hl.bind(d.mod .. " + SHIFT + T", hl.dsp.exec_cmd(d.apps.sensors))
hl.bind(d.mod .. " + G", hl.dsp.exec_cmd(d.apps.calendar))

-- Fullscreen
hl.bind(d.mod .. " + F", hl.dsp.window.fullscreen({ mode = "maximized" })) -- legacy 1
hl.bind(d.mod .. " + SHIFT + F", hl.dsp.window.fullscreen({ mode = "fullscreen" })) -- legacy 0

-- Float toggle (matches Sway mod+w)
hl.bind(d.mod .. " + W", hl.dsp.window.float({ action = "toggle" }))
hl.bind(d.mod .. " + SHIFT + W", hl.dsp.window.pin()) -- pin = sticky in Hyprland

-- Focus toggle (matches Sway mod+a = focus mode_toggle)
hl.bind(d.mod .. " + A", hl.dsp.exec_cmd("~/.config/hypr/scripts/toggle-float-focus.py"))
hl.bind(d.mod .. " + SHIFT + A", hl.dsp.group.next()) -- cycle through group windows

-- Monitor profiles (using kanshi)
hl.bind(d.mod .. " + F1", hl.dsp.exec_cmd("jumpy display main"))
hl.bind(d.mod .. " + F2", hl.dsp.exec_cmd("jumpy display main-solo"))
hl.bind(d.mod .. " + F3", hl.dsp.exec_cmd("jumpy display main-bottom"))
hl.bind(d.mod .. " + F4", hl.dsp.exec_cmd("jumpy display main-top"))
hl.bind(d.mod .. " + F5", hl.dsp.exec_cmd("jumpy display aux"))
hl.bind(d.mod .. " + F6", hl.dsp.exec_cmd("jumpy display aux-dual"))
hl.bind(d.mod .. " + F9", hl.dsp.exec_cmd("jumpy display tv"))
hl.bind(d.mod .. " + F10", hl.dsp.exec_cmd("jumpy display tv-4k"))
hl.bind(d.mod .. " + F12", hl.dsp.exec_cmd("jumpy display reload"))

-- GLOBAL SHORTCUTS
-- hyprctl globalshortcuts

-- for obsidian toggle
hl.bind("CTRL + SHIFT + O", hl.dsp.global("org.chromium.Chromium:47BF238FE34FA2C60B7414B87DA7C4AF-Ctrl+Shift+O"))
