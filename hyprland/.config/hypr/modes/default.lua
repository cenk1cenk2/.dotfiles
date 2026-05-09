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

-- New workspace (uses custom script to find empty workspace on current monitor)
hl.bind(("%s + C"):format(d.mod), hl.dsp.exec_cmd("~/.config/hypr/scripts/new-workspace.py --switch"))
hl.bind(("%s + SHIFT + C"):format(d.mod), hl.dsp.exec_cmd("~/.config/hypr/scripts/new-workspace.py --move"))
hl.bind(("%s + CTRL + SHIFT + C"):format(d.mod), hl.dsp.exec_cmd("~/.config/hypr/scripts/new-workspace.py --switch"))
hl.bind(("%s + CTRL + SHIFT + C"):format(d.mod), hl.dsp.exec_cmd(d.menu))

-- Scratchpad (special workspace)
hl.bind(("%s + D"):format(d.mod), hl.dsp.workspace.toggle_special("scratch"))
hl.bind(("%s + SHIFT + D"):format(d.mod), hl.dsp.exec_cmd("~/.config/hypr/scripts/scratchpad-toggle.py"))

-- Workspace swapping
hl.bind(("%s + CTRL + SHIFT + h"):format(d.mod), hl.dsp.exec_cmd("~/.config/hypr/scripts/swap-workspace.py -s left"))
hl.bind(("%s + CTRL + SHIFT + l"):format(d.mod), hl.dsp.exec_cmd("~/.config/hypr/scripts/swap-workspace.py -s right"))

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

-- Focus toggle (matches Sway mod+a = focus mode_toggle)
hl.bind(("%s + A"):format(d.mod), hl.dsp.exec_cmd("~/.config/hypr/scripts/toggle-float-focus.py"))
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

-- GLOBAL SHORTCUTS
-- hyprctl globalshortcuts

-- for obsidian toggle
hl.bind("CTRL + SHIFT + O", hl.dsp.global("org.chromium.Chromium:47BF238FE34FA2C60B7414B87DA7C4AF-Ctrl+Shift+O"))
