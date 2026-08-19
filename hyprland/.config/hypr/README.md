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

Common settings include `LIBSEAT_BACKEND=logind`, `WLR_XWAYLAND=/usr/local/bin/Xwayland`, Wayland Qt/GTK variables, cursor variables, `MANGOHUD=1`, `MOZ_ENABLE_WAYLAND=1`, and `DOCKER_BUILDKIT=1`.

## Hardware-Specific Sessions

Display-manager entries live in `rootfs/usr/local/share/wayland-sessions/` and select hardware profiles with UWSM's `-D` desktop list.

- **Hyprland AMD** runs `uwsm start -e -D Hyprland:Amd -- hyprland.desktop`.
- **Hyprland NVIDIA** runs `uwsm start -e -D Hyprland:Nvidia -- hyprland.desktop` and keeps NVIDIA as the default renderer/offload target.
- **Hyprland Hybrid** runs `uwsm start -e -D Hyprland:Hybrid -- hyprland.desktop` and gives Hyprland the Intel iGPU only, keeping the NVIDIA dGPU as an on-demand offload target.

`env-hybrid` detects the current `/dev/dri/card*` devices from sysfs vendor IDs at session start and exports `AQ_DRM_DEVICES` with the Intel card only. This avoids machine-specific udev rules while avoiding hard-coded card numbering in the shared dotfiles repo. The NVIDIA card is left out so the compositor renders on the iGPU; the trade-off is that outputs wired to the dGPU (the HDMI port, the muxed eDP) cannot be driven in this profile, while USB-C/DP outputs sit on the Intel card and keep working. `env-hybrid` does not export global NVIDIA PRIME/offload variables such as `__NV_PRIME_RENDER_OFFLOAD=1`, `__GLX_VENDOR_LIBRARY_NAME=nvidia`, or `GBM_BACKEND=nvidia-drm` — those are per-game launch options (native OpenGL games need `prime-run`). It sets `GSK_RENDERER=ngl` so GTK4 apps skip their Vulkan renderer's device probe, and selects the dGPU for Proton with `DXVK_FILTER_DEVICE_NAME=NVIDIA` / `VKD3D_FILTER_DEVICE_NAME=NVIDIA`.

Which device node an app holds is what decides whether the dGPU can suspend. Only `/dev/nvidia<N>` (and `/dev/nvidia-uvm`) fds count: `nvidia_open()` (`kernel-open/nvidia/nv.c`) short-circuits control-device opens straight to `nvidia_ctl_open()` and returns, so a `/dev/nvidiactl` handle never reaches `nv_start_device()` — the only path that calls `rm_ref_dynamic_power(…, NV_DYNAMIC_PM_COARSE)`, and that refcount is what pins the GPU until the fd is closed. `/dev/nvidia-caps/*` are irrelevant for the same reason, and bare DRM opens take no reference at all — `nv_drm_open()` assigns a client id and returns, so the Chromium processes that permanently hold `/dev/dri/renderD129` for enumeration (`DrmRenderNodePathFinder` in Ozone/Wayland, no NVIDIA library involved) are harmless. Fine-grained mode does not exempt an idle `/dev/nvidia0` handle: the gate in `os_ref_dynamic_power()` (`src/nvidia/arch/nvalloc/unix/src/dynamic-power.c`) is `if (mode > nvp->dynamic_power.mode) return;` over `NEVER=0, COARSE=1, FINE=2`, so a COARSE ref taken at open still counts while FINE is configured. The clean result — reached in normal sessions, where `/dev/nvidia0` is not even created — is `tdp nvidia show` reporting zero pinning holders; `nvidiactl` and `renderD*` rows are expected.

Chromium-based apps (Brave, Slack, Spotify, Electron) held `/dev/nvidia0` through MangoHud, not through anything Chromium does itself: MangoHud installs an *implicit* Vulkan layer, so the loader injects `libMangoHud.so` into any process that creates an instance — Chromium's startup GPU probe qualifies even with `--disable-features=Vulkan` — and layer init unconditionally dlopens `libnvidia-ml`, whose NVML init opens the device node. MangoHud's `blacklist=` config does not help; it only suppresses the overlay, long after the library and NVML have loaded. `MANGOHUD=0` is the only lever that stops it.

Hyprland and hyprpaper hold `/dev/nvidia0` for the whole session through GLVND's EGL vendor enumeration, which loads every vendor in `/usr/share/glvnd/egl_vendor.d/` merely to query it — and `libEGL_nvidia` opens the node as it initializes. Pinning `__EGL_VENDOR_LIBRARY_FILENAMES` to Mesa removes those holders (measured: 16 opens with NVIDIA listed, 0 with Mesa alone), and `MANGOHUD=0` removes the Chromium/Electron ones. **Both are deliberately left unset.** They only mattered as a route to D3cold, which the driver bug below makes unreachable anyway, and the EGL pin costs gaming outright: `winewayland.drv` presents through EGL, so with NVIDIA EGL unavailable Wine hands DXVK a one-device list and every Proton title runs on the iGPU. Unset, Wine enumerates both and the device filters pick the dGPU. Re-enable the pair only if a driver update restores runtime suspend, and expect to trade Proton's dGPU access for it.

The local `prime-run` (`scripts/.local/bin/prime-run`, shadowing `/usr/bin/prime-run` since `~/.local/bin` precedes `/usr/bin` in `PATH`) adds `MANGOHUD=1` to the packaged script's three offload variables. It is for **directly launched** native titles only.

**Steam games do not go through it, and do not need it.** The steam-runtime launcher resets `PATH` to `…/pv-runtime/steam-runtime-steamrt/bin:/usr/bin:/bin`, so `prime-run %command%` resolves to `/usr/bin/prime-run` — which never hands `MANGOHUD=1` back, hence no overlay. Its `__VK_LAYER_NV_optimus=NVIDIA_only` is inert as well: pressure-vessel does not import `nvidia_layers.json` into the container (only the MangoHud layers are present), so `VK_LAYER_NV_optimus` cannot load and cannot filter the device list. Left alone, DXVK takes the first adapter Wine offers and lands on the iGPU. `env-hybrid` fixes this session-wide with `DXVK_FILTER_DEVICE_NAME=NVIDIA` and `VKD3D_FILTER_DEVICE_NAME=NVIDIA`, which the translation layers read themselves inside the container; the DXVK log then shows `Found device: Intel… Skipping: Device filter` followed by `Found device: NVIDIA GeForce RTX 5070 Laptop GPU`. Diagnose adapter problems with `PROTON_LOG=1 %command%`, which writes `~/steam-<appid>.log`. For the overlay, add `MANGOHUD=1 %command%` as the launch option; native OpenGL titles still need the `mangohud` wrapper for its `LD_PRELOAD`, chained as `prime-run mangohud <game>`.

**Open problem — RM never re-indicates idle.** With every userspace pin fixed, the GPU suspends only until the session's first wake of the device, then never again: the driver's runtime-PM usage count sticks at 1 with zero holders (probe: toggle `power/control` on→auto with `rpm:*` ftrace events enabled and read the `cnt-` field; zero `rpm:*` events during idle is the same signature). The decision lives in RM core (`nv-kernel.o`, shipped prebuilt) and GSP firmware, where the GCx entry prerequisite (`NV2080_CTRL_CMD_INTERNAL_GCX_ENTRY_PREREQUISITE`) is evaluated; the module is compiled notrace so kprobes are rejected, and release builds compile the relevant prints out. Identical on 610.43.03 and 610.57.04; `nvidia-drm.fbdev=0` makes no difference. Suspected GB206M driver bug — re-test on each driver update. Until fixed the dGPU idles at P8 (~5 W) after its first wake; for a genuinely powered-off dGPU use `tdp nvidia drain` / `tdp nvidia remove` on demand.

Check holders wake-free with `tdp nvidia show`: it resolves device names from sysfs IDs plus `/usr/share/hwdata/pci.ids` and locates holders with `fuser`, so it only ever `stat()`s the device nodes — verified under `strace` as zero `openat` on `/dev/nvidia*` and no PCI config-space reads. `--smi` is the opt-in that wakes the GPU; `nvidia-smi`, `btop`, and `lspci` all wake it unconditionally.

dGPU runtime power management support lives in `rootfs/`:
- `etc/modprobe.d/nvidia-power.conf` — `NVreg_DynamicPowerManagement=0x03` (equals the built-in default, pinned because `nv_allow_runtime_suspend()` is gated on the regkey being exactly DEFAULT — an explicit `0x02` skips that path) and `NVreg_EnableS0ixPowerManagement=1`. `NVreg_DynamicPowerManagementVideoMemoryThreshold` stays at its default (200): it is the GCOFF cap in MB (`usedFbSize <= threshold` required in `RmCanEnterGcxUnderGpuLock`), so setting it to 0 forbids full power-off permanently once RM's ~2MiB is allocated. This file is **copied** to `/etc` by the Taskfile — edits need a re-install; the udev rules below are stow-symlinked and live on save.
- `etc/udev/rules.d/80-nvidia-pm.rules` — runtime PM `auto` for the GPU's main PCI function (`0x030000`) and its HDMI audio function (`0x040300`), which otherwise blocks RTD3.
- `nvidia-persistenced.service` is **enabled, and required for Proton to render on the dGPU**. Without it the kernel tears down device state whenever no client holds the GPU, and vkd3d-proton then fails swapchain creation with `VK_ERROR_UNKNOWN` (-13) in `dxgi_vk_swap_chain_recreate_swapchain_in_present_task`, followed by a `vkGetPastPresentationTimingEXT` access violation — surfacing as a crash a few seconds after launch, or as an endless swapchain-retry loop that looks like a black screen. Rendering and adapter selection are fine either way; only presentation breaks, which is why it looks intermittent. The daemon pins the GPU awake (it opens `/dev/nvidia0` before enabling SW persistence, so its own fds hold a COARSE ref for its lifetime), and that costs nothing while the RM idle bug below makes sleep unreachable anyway. Never toggle the daemon or `nvidia-smi -pm` live — the persistence flag flips against in-flight power refs and the refcount desyncs silently until reboot (signature: zero `rpm:*` trace events with zero holders).

Use `Hyprland NVIDIA` when the whole desktop should run on NVIDIA or the HDMI port must drive a monitor. Use `Hyprland Hybrid` for laptop sessions: Intel drives all displays, the dGPU sleeps when idle and serves offloaded games.

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

`$mod + R` enters recording mode — screen recording, speech-to-text,
text-to-speech and the copywriter all live in this one submap (mirrors the sway
recording mode; there is no separate AI mode).

### Screen Recording

- **r** - Toggle recording (start/stop)
- **R** (Shift+r) - Pause/resume recording
- **o** - Open OBS window
- **Q** (Shift+q) - Stop recording
- **z** - Toggle zoom (hypr-zoom)

### Speech-to-Text (stt)

- **s** - stt → type, AI-enriched
- **S** (Shift+s) - stt → type, raw
- **c** - stt → clipboard, AI-enriched
- **C** (Shift+c) - stt → clipboard, raw
- **q** - Stop stt

### Text-to-Speech (tts)

- **t** - tts play/stop — reads the clipboard aloud, stops it when already speaking
- **T** (Shift+t) - Stop speaking

### Copywriter

- **w** - Refine clipboard through the copywriter
- **W** (Shift+w) - Kill copywriter

### General

- **Esc** - Exit recording mode

Recording is driven by `recorder.py`, speech-to-text by `speech.py stt` (the
`--enrich` variants pass the transcript through the configured LLM enricher
first), text-to-speech by `speech.py tts` (Kokoro through speaches, streamed as
raw PCM into `ffplay`), and clipboard refinement by `copywriter.py`.

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

- `$mod + Shift + R` - Resize mode
- `$mod + S` - Screenshot mode
- `$mod + R` - Recording mode

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
