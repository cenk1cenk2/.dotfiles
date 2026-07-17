# Hyprland Configuration

Modular Hyprland configuration with feature parity to Sway setup.

## Structure

```
hyprland/.config/hypr/
├── hyprland.conf              # Main configuration file
├── hyprpaper.conf             # Wallpaper daemon configuration
├── hyprqt6engine.conf         # Qt6 platform theme configuration
├── application-style.conf     # Qt application styling
├── definitions.conf           # User variables and command definitions
├── themes/custom/
│   └── definitions.conf       # Theme colors, fonts, GTK settings
├── inputs/
│   ├── default-keyboard.conf  # Keyboard configuration (us,de,tr layouts)
│   └── default-touchpad.conf  # Touchpad and device-specific settings
├── outputs/
│   └── default-screen.conf    # Monitor configuration (with kanshi)
├── modes/
│   ├── resize.conf            # Resize mode (submap)
│   └── screenshot.conf        # Screenshot mode (submap)
├── config.d/
│   ├── 00-default.conf        # Default keybindings
│   ├── 50-systemd-user.conf   # Systemd integration
│   ├── 90-theming.conf        # GTK theme application
│   ├── 98-window-rules.conf   # Window rules
│   └── 99-autostart.conf      # Autostart applications
└── scripts/                   # Helper scripts (to be ported)
```

## Environment Variables

Environment variables are prepared by UWSM before Hyprland starts. Keep compositor, toolkit, and GPU selection variables in `uwsm/.config/uwsm/env*` so they reach Hyprland and the user systemd environment.

UWSM loads the common file plus profile files selected by the display-manager session:

- `uwsm/.config/uwsm/env` - common Wayland/toolkit/application environment.
- `uwsm/.config/uwsm/env-hyprland` - Hyprland desktop identity and Hyprcursor environment.
- `uwsm/.config/uwsm/env-amd` - AMD media acceleration profile.
- `uwsm/.config/uwsm/env-nvidia` - NVIDIA-default profile.
- `uwsm/.config/uwsm/env-hybrid` - Intel-driven compositor with NVIDIA as offload-only profile.
- `uwsm/.config/uwsm/env-integrated` - integrated-GPU-only profile.

Common settings include `LIBSEAT_BACKEND=logind`, `WLR_XWAYLAND=/usr/local/bin/Xwayland`, Wayland Qt/GTK variables, cursor variables, `MANGOHUD=1`, `MOZ_ENABLE_WAYLAND=1`, and `DOCKER_BUILDKIT=1`.

## Hardware-Specific Sessions

Display-manager entries live in `rootfs/usr/local/share/wayland-sessions/` and select hardware profiles with UWSM's `-D` desktop list.

- **Hyprland AMD** runs `uwsm start -e -D Hyprland:Amd -- hyprland.desktop`.
- **Hyprland NVIDIA** runs `uwsm start -e -D Hyprland:Nvidia -- hyprland.desktop` and keeps NVIDIA as the default renderer/offload target.
- **Hyprland Hybrid** runs `uwsm start -e -D Hyprland:Hybrid -- hyprland.desktop` and gives Hyprland the Intel iGPU only, keeping the NVIDIA dGPU as an on-demand offload target.
- **Hyprland Integrated** runs `uwsm start -e -D Hyprland:Integrated -- hyprland.desktop` and exposes only the integrated GPU to Hyprland.

`env-hybrid` detects the current `/dev/dri/card*` devices from sysfs vendor IDs at session start and exports `AQ_DRM_DEVICES` with the Intel card only. This avoids machine-specific udev rules while avoiding hard-coded card numbering in the shared dotfiles repo. The NVIDIA card is deliberately left out: a compositor holding the dGPU's KMS node keeps it permanently active and defeats fine-grained RTD3 runtime suspend, while Vulkan device enumeration ignores `AQ_DRM_DEVICES` entirely — DXVK/vkd3d-proton pick the discrete GPU on their own and the driver wakes it from suspend on demand. The trade-off is that outputs wired to the dGPU (the HDMI port, the muxed eDP) cannot be driven in this profile; USB-C/DP outputs sit on the Intel card and keep working. `env-hybrid` does not export global NVIDIA PRIME/offload variables such as `__NV_PRIME_RENDER_OFFLOAD=1`, `__GLX_VENDOR_LIBRARY_NAME=nvidia`, or `GBM_BACKEND=nvidia-drm` — those are per-game launch options (native OpenGL games need `prime-run`). It does restrict EGL to the Mesa vendor (`__EGL_VENDOR_LIBRARY_FILENAMES`): GLVND's EGL enumeration otherwise opens `/dev/nvidia0` in every EGL application, and those handles pin the dGPU awake even when idle. It also sets `GSK_RENDERER=ngl` so GTK4 apps skip their Vulkan renderer's device probe. Vulkan enumeration and GLX offload are separate paths and keep working.

Known residual dGPU holders that no configuration fixes: current Chromium bases (Brave) probe NVML and GBM from the main browser process with no opt-out, and slack-desktop/spotify bundle their own engines that read no flags files — the dGPU sleeps only while those apps are closed. Check holders wake-free with `tdp nvidia show`.

dGPU runtime power management support lives in `rootfs/`:
- `etc/modprobe.d/nvidia-power.conf` — `NVreg_DynamicPowerManagement=0x02` (fine-grained RTD3 — `0x03` is the default but Blackwell GPUs need `0x02` explicitly; safe on desktop where RTD3 is disabled regardless), `NVreg_EnableS0ixPowerManagement=1`, `NVreg_PreserveVideoMemoryAllocations=0` (allow suspend while GPU active), `NVreg_DynamicPowerManagementVideoMemoryThreshold=0` (keep VRAM in self-refresh — workaround for NVIDIA issue #905 where the GPU enters D3cold but immediately wakes in a loop).
- `etc/udev/rules.d/80-nvidia-pm.rules` — runtime PM `auto` for the GPU's main PCI function (`0x030000`) and its HDMI audio function (`0x040300`), which otherwise blocks RTD3.
- `nvidia-persistenced.service` should be enabled on the host (not tracked in dotfiles — it's a systemd unit from `nvidia-utils`).

Use `Hyprland NVIDIA` when the whole desktop should run on NVIDIA or the HDMI port must drive a monitor. Use `Hyprland Hybrid` for laptop sessions: Intel drives all displays, the dGPU sleeps when idle and serves offloaded games. Use `Hyprland Integrated` when the dGPU should stay invisible to applications as well, so `tdp nvidia remove` can remove it entirely.

## Tool Choices

### Core Components

- **Compositor**: Hyprland
- **Wallpaper**: hyprpaper (official Hyprland)
- **Idle Daemon**: hypridle (official Hyprland)
- **Lock Screen**: hyprlock (official Hyprland)
- **Clipboard Manager**: clipse
- **Monitor Hotplug**: kanshi (kept from Sway config)
- **Notification Daemon**: swaync
- **Status Bar**: waybar
- **OSD**: swayosd

### Compositor-Agnostic Tools (Work with both Sway and Hyprland)

- **Autotiling**: autotiling-rs
- **Screenshots**: grim, slurp, swappy
- **Clipboard**: wl-copy, wl-paste
- **Launcher**: rofi
- **Terminal**: kitty
- **Gamma Control**: wl-gammarelay-rs
- **Media Control**: playerctl
- **File Manager**: thunar
- **Power Alert**: poweralertd
- **Input Remapping**: input-remapper

### Hyprland-Specific Additions

- **Workspace Naming**: hyprland-autoname-workspaces (replaces sworkstyle)
- **Window Switcher**: rofi with hyprctl (replaces swayr)
- **Qt Theming**: hyprqt6engine (Qt6 platform theme)
- **Qt Application Styling**: hyprland-qt-support (UI styling for Qt apps)
- **Polkit Agent**: hyprpolkitagent (authentication agent)
- **PipeWire Control**: hyprpwcenter (GUI audio control center)
- **Cursor Theme**: hyprcursor (native Wayland cursor support, replaces xcursor)

## Qt Application Theming

### hyprqt6engine

Qt6 platform theme configured in `hyprqt6engine.conf`:

- Sets fonts (GUI and monospace)
- Icon theme integration
- Widget style (Fusion recommended)
- Color scheme support
- Menu and shortcut display settings

Environment variable `QT_QPA_PLATFORMTHEME=hyprqt6engine` is set in main config.

### hyprland-qt-support

Application styling configured in `application-style.conf`:

- **roundness** (0-3): UI element rounding level
- **border_width** (0-3): Border thickness
- **reduce_motion** (true/false): Disable transitions/hover effects

## Cursor Configuration

### hyprcursor

Hyprcursor provides native Wayland cursor support with improved performance over xcursor.

Configuration in `config.d/90-theming.conf`:

```hyprlang
# Environment variables
env = HYPRCURSOR_THEME, $cursor-theme
env = HYPRCURSOR_SIZE, 24

# Native cursor settings
cursor {
    no_hardware_cursors = false
    enable_hyprcursor = true
}
```

- **HYPRCURSOR_THEME**: Cursor theme name (uses `$cursor-theme` variable from theme definitions)
- **HYPRCURSOR_SIZE**: Cursor size in pixels (default: 24)
- **enable_hyprcursor**: Enable native hyprcursor support (true)
- **no_hardware_cursors**: Disable hardware cursors if needed (false by default)

The cursor theme is also set via gsettings for GTK application compatibility.

## Wallpaper Configuration

hyprpaper is configured in `hyprpaper.conf`:

- Preload wallpapers before setting them
- Set per-monitor or all monitors
- IPC enabled for dynamic wallpaper changes
- Use `hyprctl hyprpaper reload` to change wallpapers on the fly

Example:

```bash
# Change wallpaper for all monitors
hyprctl hyprpaper reload ,"~/new-wallpaper.png"

# Change wallpaper for specific monitor
hyprctl hyprpaper reload "DP-1,~/new-wallpaper.png"
```

## Scratchpad

The scratchpad is a hidden workspace (special workspace) where you can temporarily store windows and toggle them on/off with a single keybind.

**Usage:**

- Press `$mod + Shift + D` to send any window to the scratchpad
- Press `$mod + D` to toggle the scratchpad (show/hide all windows in it)
- Multiple windows can exist in the scratchpad - they all appear/disappear together
- Windows in the scratchpad overlay on top of your current workspace

**Use cases:**

- Quick access to terminal, music player, or notes
- Temporary storage for windows you want to hide but keep running
- Multi-monitor workflows where you want windows available on any screen

The scratchpad uses Hyprland's native `special:scratch` workspace feature - no plugins required.

## Recording Mode

The recording mode provides quick access to screen recording and speech-to-text features:

### Screen Recording

- **r** - Toggle recording (start/stop)
- **p** - Pause/resume recording
- **o** - Open OBS window
- **q** - Stop recording

### Speech-to-Text

- **s** - Speech-to-text to clipboard (wl-copy)
- **S** (Shift+s) - Speech-to-text direct typing (ydotool)
- **Q** (Shift+q) - Cancel speech recording

### General

- **Esc** - Exit recording mode

Recordings are managed via the recorder script. Speech-to-text uses `hyprwhspr` with Whisper AI for voice transcription via OpenWebUI.

## Scripts to Port

The following scripts still need migration:

1. `swap-workspace.sh` - Workspace swapping functionality (optional advanced feature)
2. `upload-image.sh` - Screenshot upload

## Migrated Scripts

The following scripts have been successfully ported to Hyprland:

- ✅ `recorder.py` - Screen recording with wl-screenrec
- ✅ `grimshot.py` - Screenshot utility using hyprctl instead of swaymsg
- ✅ `display-profile.py` - Monitor profile switching using kanshictl (compositor-agnostic)
- ✅ `new-workspace.py` - Replaced with native Hyprland `empty` workspace dispatcher

## Keybindings Reference

### Main Modifier

- `$mod` = SUPER key

### Essential

- `$mod + Return` - Terminal (kitty)
- `$mod + Space` - Launcher (rofi)
- `$mod + P` - Clipboard (clipse)
- `$mod + Shift + Q` - Close window
- `$mod + Ctrl + Shift + W` - Reload config

### Focus Movement

- `$mod + h/j/k/l` - Focus left/down/up/right (vim keys)
- `$mod + arrows` - Focus monitor in direction

### Window Movement

- `$mod + Shift + h/j/k/l` - Move window
- `$mod + Shift + arrows` - Move to monitor

### Workspaces

- `$mod + 1-9/0` - Switch workspace
- `$mod + Shift + 1-9/0` - Move window to workspace
- `$mod + Ctrl + arrows` - Next/prev workspace on monitor
- `$mod + C` - Go to empty workspace
- `$mod + Shift + C` - Move window to empty workspace

### Scratchpad

- `$mod + D` - Toggle scratchpad (show/hide all scratchpad windows)
- `$mod + Shift + D` - Move current window to scratchpad

### Modes

- `$mod + R` - Resize mode
- `$mod + S` - Screenshot mode
- `$mod + Shift + R` - Recording mode

### Quick Access

- `$mod + N` - Toggle notifications
- `$mod + M` - Audio mixer
- `$mod + T` - Process manager
- `$mod + F1-F12` - Monitor profiles

## Migration from Sway

Key differences:

1. **Wallpaper**: swaybg → hyprpaper with config file
2. **Modes → Submaps**: Sway modes are Hyprland submaps
3. **Window Rules**: `for_window` → `windowrulev2`
4. **IPC**: `swaymsg` → `hyprctl`
5. **Exec**: `exec` → `exec-once`, `exec_always` → `exec`

## Color Scheme

Based on Base16 Seti UI with Catppuccin-inspired accents.

## Notes

- Device-specific input configs may need adjustment based on `hyprctl devices`
- Monitor configuration handled by kanshi
- GTK theme integration via xsettingsd and gsettings
- Systemd session management for clean startup/shutdown
- hyprpaper IPC enabled for dynamic wallpaper management
