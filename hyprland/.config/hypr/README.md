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

Environment variables are configured directly in `hyprland.conf` using Hyprland's native `env` directive:

### Session and Desktop

- `XDG_SESSION_TYPE=wayland`
- `XDG_CURRENT_DESKTOP=Hyprland`
- `LIBSEAT_BACKEND=logind`
- `PATH` - Includes mise shims (`~/.local/share/mise/shims`) and user bin (`~/.local/bin`)

### Wayland/XWayland

- `WLR_RENDERER_ALLOW_SOFTWARE=1` - Allow software rendering
- `WLR_XWAYLAND=/usr/local/bin/Xwayland` - Custom Xwayland path

### Qt Configuration

- `QT_QPA_PLATFORM=wayland` - Use Wayland backend
- `QT_QPA_PLATFORMTHEME=hyprqt6engine` - Use Hyprland Qt theme
- `QT_WAYLAND_DISABLE_WINDOWDECORATION=1` - Disable Qt decorations

### GTK Configuration

- `GTK_USE_PORTAL=1` - Use XDG portals
- `GDK_BACKEND=wayland` - Use Wayland backend
- `GDK_DEBUG=portals` - Enable portal debugging

### Gaming Optimizations

- `DXVK_ASYNC=1` - Enable async DXVK shader compilation
- `MANGOHUD=1` - Enable MangoHUD overlay

### Applications

- `MOZ_ENABLE_WAYLAND=1` - Firefox Wayland support
- `MOZ_DBUS_REMOTE=1` - Firefox D-Bus remote
- `DOCKER_BUILDKIT=1` - Enable Docker BuildKit

## Hardware-Specific Configuration

Hardware-specific environment variables (GPU-related) are handled by wrapper scripts in `rootfs/usr/local/bin/`:

- **hyprland-amd** - Launch Hyprland for AMD systems
- **hyprland-nvidia** - Launch Hyprland for NVIDIA systems with additional GPU-specific variables:
  - GBM backend settings
  - PRIME render offload
  - VDPAU and VAAPI driver configuration
  - Proton/DXVK NVAPI settings
  - VKD3D DirectX Raytracing support

Use the appropriate script when launching Hyprland from your display manager or `.xinitrc`.

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

## Recording Mode

The recording mode provides quick access to screen recording features:

- **r** - Record full screen (focused output) to file
- **Shift + r** - Record full screen with audio
- **s** - Record selected region
- **Shift + s** - Record selected region with audio
- **q** - Kill/stop recording
- **Esc** - Exit recording mode

Recordings are saved to your Videos directory (via `xdg-user-dir VIDEOS`) with timestamp filenames.

Uses `wl-screenrec` for recording and `slurp` for region selection.

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
