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

`env-hybrid` detects the current `/dev/dri/card*` devices from sysfs vendor IDs at session start and exports `AQ_DRM_DEVICES` with the Intel card only. This avoids machine-specific udev rules while avoiding hard-coded card numbering in the shared dotfiles repo. The NVIDIA card is deliberately left out: a compositor holding the dGPU's KMS node keeps it permanently active and defeats fine-grained RTD3 runtime suspend, while Vulkan device enumeration ignores `AQ_DRM_DEVICES` entirely — DXVK/vkd3d-proton pick the discrete GPU on their own and the driver wakes it from suspend on demand. The trade-off is that outputs wired to the dGPU (the HDMI port, the muxed eDP) cannot be driven in this profile; USB-C/DP outputs sit on the Intel card and keep working. `env-hybrid` does not export global NVIDIA PRIME/offload variables such as `__NV_PRIME_RENDER_OFFLOAD=1`, `__GLX_VENDOR_LIBRARY_NAME=nvidia`, or `GBM_BACKEND=nvidia-drm` — those are per-game launch options (native OpenGL games need `prime-run`). It does restrict EGL to the Mesa vendor (`__EGL_VENDOR_LIBRARY_FILENAMES`), sets `GSK_RENDERER=ngl` so GTK4 apps skip their Vulkan renderer's device probe, and sets `MANGOHUD=0` so the implicit MangoHud Vulkan layer stops dlopening NVML. Vulkan enumeration and GLX offload are separate paths and keep working — the dGPU still enumerates for DXVK/vkd3d, and `prime-run glxinfo` still reports NVIDIA.

Which device node an app holds is what decides whether the dGPU can suspend. `/dev/nvidia0` is one of two that count; the dGPU's DRM render node (`/dev/dri/renderD129`) is the other, and GBM/dmabuf clients hold that one rather than `/dev/nvidia0` — `libnvidia-allocator` opens it even after the EGL and MangoHud levers above are applied. `nvidia_open()` (`kernel-open/nvidia/nv.c`) short-circuits control-device opens straight to `nvidia_ctl_open()` and returns, so a `/dev/nvidiactl` handle never reaches `nv_start_device()` — the only path that calls `rm_ref_dynamic_power(…, NV_DYNAMIC_PM_COARSE)`, and that refcount is what pins the GPU until the fd is closed. `/dev/nvidia-caps/*` are irrelevant for the same reason. Fine-grained mode does not exempt an idle handle: the gate in `os_ref_dynamic_power()` (`src/nvidia/arch/nvalloc/unix/src/dynamic-power.c`) is `if (mode > nvp->dynamic_power.mode) return;` over `NEVER=0, COARSE=1, FINE=2`, so a COARSE ref taken at open still counts while FINE is configured. One open fd on `/dev/nvidia0`, from anything, for any duration, holds the GPU in D0 with zero contexts and zero allocated VRAM — by design, and distinct from issue #905 below. A `/dev/dri/renderD129` handle behaves the same way — `nvidia_dev_get()` is the in-kernel analog of `nvidia_open()` used by `nvidia-drm`, and it takes a `NV_DYNAMIC_PM_FINE` reference, which also counts while fine-grained is configured. So the clean result is `tdp nvidia show` listing only `/dev/nvidiactl` holders; anything on `/dev/nvidia0` **or** `/dev/dri/renderD129` pins the GPU.

Chromium-based apps (Brave, Slack, Spotify, Electron) held `/dev/nvidia0` through MangoHud, not through anything Chromium does itself: MangoHud installs an *implicit* Vulkan layer, so the loader injects `libMangoHud.so` into any process that creates an instance — Chromium's startup GPU probe qualifies even with `--disable-features=Vulkan` — and layer init unconditionally dlopens `libnvidia-ml`, whose NVML init opens the device node. MangoHud's `blacklist=` config does not help; it only suppresses the overlay, long after the library and NVML have loaded. `MANGOHUD=0` is the only lever that stops it.

Hyprland and hyprpaper held the node for the whole session through GLVND's EGL vendor enumeration, which loads every vendor listed in `__EGL_VENDOR_LIBRARY_FILENAMES` merely to query it — and `libEGL_nvidia` opens `/dev/nvidia0` as it initializes. Listing NVIDIA at all pins the dGPU regardless of which vendor is finally selected (measured: 16 opens with NVIDIA listed, 0 with Mesa alone), so the restriction has to be session-wide rather than scoped. Restricting it is not free: `prime-run` only flips the *GLX* vendor selector, so under Mesa-only EGL an offloaded process silently renders on the iGPU instead of failing — `prime-run eglinfo` drops from `NVIDIA GeForce RTX 5070` to `Mesa Intel(R) Graphics`.

Both variables are therefore handed back by a local `prime-run` (`scripts/.local/bin/prime-run`, shadowing `/usr/bin/prime-run` since `~/.local/bin` precedes `/usr/bin` in `PATH`), which adds `__EGL_VENDOR_LIBRARY_FILENAMES` with both vendors and `MANGOHUD=1` to the packaged script's three offload variables. Games are launched through it — as a Steam per-title launch option (`prime-run %command%`) or directly. Vulkan-only titles reach the dGPU without it; they just render EGL on the iGPU and lose the overlay. OpenGL overlays additionally need the `mangohud` wrapper for its `LD_PRELOAD`, chained as `prime-run mangohud <game>`.

**Open problem — `/dev/dri/renderD129`.** With both levers applied, Chromium-based apps still open the dGPU's render node from the main process *and* the gpu-process, so the profile does not currently reach D3cold. Constraints on any fix: per-application flags are unusable (this config is shared across machines, and Slack/Spotify bundle their own Chromium and read no flags file), which leaves session environment variables and system-level mechanisms only.

Measured and ruled out for `renderD129`:

| Lever | Result |
|---|---|
| `GBM_BACKENDS_PATH` at a Mesa-only dir | no effect |
| `DRI_PRIME=0`, `DRI_PRIME=0!` | no effect |
| `__EGL_VENDOR_LIBRARY_FILENAMES` Mesa-only | no effect (this node is not reached through GLVND) |
| `MANGOHUD=0` | no effect (fixes `/dev/nvidia0` only) |
| systemd `DevicePolicy=closed` + `DeviceAllow` | blocks `renderD129`, but also kills Intel — brave starts with zero DRM fds even with `renderD128 rw` explicitly allowed |
| `nvidia_drm modeset=0` / `fbdev=0` | cannot help. `DRIVER_GEM \| DRIVER_RENDER` are set unconditionally in `nv_drm_driver`'s static initializer; the `modeset` param only *adds* `DRIVER_MODESET \| DRIVER_ATOMIC`, and `fbdev` gates only console takeover. The render minor is registered either way |
| Chromium flags (`--render-node-override`) | would work per Chromium source, and propagates to child processes — but unusable here: the flags files are shared across machines, and Slack/Spotify/Obsidian bundle their own Chromium and read no flags file. The `/usr/bin/brave` wrapper offers no env-var injection either |

The reason no environment variable can work: with `VK_DRIVER_FILES` pinned to the Intel ICD the gpu-process drops to `renderD128`, but the **main** process still holds `renderD129` with zero NVIDIA libraries mapped — it is enumerating `/dev/dri/renderD*` by path directly (`DrmRenderNodePathFinder` in Chromium's Ozone/Wayland layer). There is no library in that loop to redirect.

Denying the node at the DAC level (`chmod 0600`, or a udev `MODE`/`GROUP` rule matching `SUBSYSTEM=="drm", KERNEL=="renderD*", DRIVERS=="nvidia"`) does stop it — Chromium falls back to `renderD128` cleanly with no crash, and being kernel-enforced it reaches the bundled-Chromium apps. It is not adopted: keeping `prime-run` EGL offload working alongside it needs a dedicated group plus a privileged helper (setgid binary or sudoers rule, since the kernel ignores setgid on `#!` scripts), which is disproportionate machinery for the problem.

**Current status: unsolved.** The dGPU idles at P8 (~6 W) rather than D3cold whenever a Chromium/Electron app is running. For a genuinely powered-off dGPU use `tdp nvidia drain` / `tdp nvidia remove` on demand. Upstream, `issues.chromium.org/337870536` ("Chromium on Wayland always uses the dGPU, significantly reduces battery life") tracks this symptom class.

Check holders wake-free with `tdp nvidia show`: it resolves device names from sysfs IDs plus `/usr/share/hwdata/pci.ids` and locates holders with `fuser`, so it only ever `stat()`s the device nodes — verified under `strace` as zero `openat` on `/dev/nvidia*` and no PCI config-space reads. `--smi` is the opt-in that wakes the GPU; `nvidia-smi`, `btop`, and `lspci` all wake it unconditionally.

dGPU runtime power management support lives in `rootfs/`:
- `etc/modprobe.d/nvidia-power.conf` — `NVreg_DynamicPowerManagement=0x03` (default — fine-grained RTD3 on Ampere+ notebooks, disabled on desktop), `NVreg_EnableS0ixPowerManagement=1`, `NVreg_PreserveVideoMemoryAllocations=0` (allow suspend while GPU active), `NVreg_DynamicPowerManagementVideoMemoryThreshold=0` (keep VRAM in self-refresh rather than powering it off). Note this is **not** the issue-#905 workaround it was originally added as: the threshold gates only the GCOFF fallback path (`RmCanEnterGcxUnderGpuLock` in `dynamic-power.c`), and that branch is skipped entirely while `PDB_PROP_GPU_RTD3_GC6_SUPPORTED` holds — which it does here (`Runtime D3 status: Enabled (fine-grained)`, GC6 Supported). It neither blocks nor enables D3cold entry on this machine; it only selects self-refresh over memory-off. #905 itself is an ACPI GPE firmware bug on specific Legion SKUs whose signature is rapid D0↔D3cold oscillation, which this machine does not exhibit.
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

`$mod + R` enters recording mode — screen recording, speech-to-text, and the
copywriter all live in this one submap (mirrors the sway recording mode; there
is no separate AI mode).

### Screen Recording

- **r** - Toggle recording (start/stop)
- **R** (Shift+r) - Pause/resume recording
- **o** - Open OBS window
- **Q** (Shift+q) - Stop recording
- **z** - Toggle zoom (hypr-zoom)

### Speech-to-Text

- **s** - Speech → type, AI-enriched
- **S** (Shift+s) - Speech → type, raw
- **c** - Speech → clipboard, AI-enriched
- **C** (Shift+c) - Speech → clipboard, raw
- **q** - Stop speech-to-text

### Copywriter

- **w** - Refine clipboard through the copywriter
- **W** (Shift+w) - Kill copywriter

### General

- **Esc** - Exit recording mode

Recording is driven by `recorder.py`, speech-to-text by `speech.py` (the
`--enrich` variants pass the transcript through the configured LLM enricher
first), and clipboard refinement by `copywriter.py`.

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
