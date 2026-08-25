#!/usr/bin/env bash
# One-time cleanup after the 2026-08 migration.
#
# That migration retired the macOS target and the sway stack, and moved
# rootfs/ off stow onto install.py. Neither stow nor install.py deletes
# anything, so every machine that deployed the old layout still carries the
# leftovers listed below. Run it on each machine after pulling; it has grown
# past a single migration, so re-run it when this file changes rather than
# deleting it after one pass.
#
# Everything here is idempotent: an entry that is already gone is skipped.
#
# Usage:
#   ./cleanup.sh        show what would happen
#   ./cleanup.sh -y     do it

set -euo pipefail

apply=0
[ "${1:-}" = "-y" ] && apply=1

# Deployed symlinks whose package no longer exists in the repo.
USER_PATHS=(
    # sway and its helpers
    "$HOME/.config/sway"
    "$HOME/.config/swaylock"
    "$HOME/.config/swayr"
    "$HOME/.config/sworkstyle"
    "$HOME/.config/pacman/sway.txt"
    "$HOME/.config/uwsm/env-sway"
    "$HOME/.config/waybar/sway.jsonc"
    "$HOME/.config/xdg-desktop-portal/sway-portals.conf"
    "$HOME/.config/systemd/user/autotiling-rs.service"
    "$HOME/.config/systemd/user/swayidle.service"
    "$HOME/.config/systemd/user/swayrd.service"
    "$HOME/.config/systemd/user/sworkstyle.service"
    "$HOME/.config/systemd/user/wl-gammarelay-rs.service"
    "$HOME/.config/systemd/user/waybar@sway.service.d"
    # retired earlier in the same migration. NOT ~/.config/google-chrome --
    # the repo's symlinks under it are already gone, but the directory holds
    # native-messaging manifests written by Bitwarden, Claude and Granted.
    "$HOME/.config/chrome-flags.conf"
    "$HOME/.config/systemd/user/dex.service"
    # mako, replaced by swaync
    "$HOME/.config/mako"
    # lib modules that moved into the shared dotlib package. stow never removes
    # a link whose source left the repo, so these dangle after the migration.
    # notify.py already dangles, from commit 035fae7.
    "$HOME/.config/hypr/scripts/lib/cli.py"
    "$HOME/.config/hypr/scripts/lib/notify.py"
    "$HOME/.config/wayland/scripts/lib/cli.py"
    "$HOME/.config/wayland/scripts/lib/desktop.py"
    "$HOME/.config/wayland/scripts/lib/notify.py"
    "$HOME/.local/bin/lib"
    # hyprctl briefly lived in dotlib before moving back to the hyprland
    # project, so a machine that deployed in between has this dangling.
    "$HOME/.config/wayland/lib/src/dotlib/hyprctl.py"
    # the kitty platform split collapsed to a single globinclude
    "$HOME/.config/kitty/linux.conf"
    "$HOME/.config/kitty/macos.conf"
)

# Real files install.py had already copied, so removing the source left them
# behind. The sway entries still show sway in the greeter, pointing at a
# binary that is no longer installed.
SYSTEM_PATHS=(
    /usr/local/share/wayland-sessions/sway-amd-uwsm.desktop
    /usr/local/share/wayland-sessions/sway-nvidia-uwsm.desktop
    /etc/systemd/system/sysctl-dotfiles.service
    /etc/tlp.conf.pacsave
    /usr/local/bin/__pycache__
    # jumpy moved to ~/.local/bin. Remove ONLY after home-assistant!69 merges —
    # until then this is what makes the old bare `jumpy` over ssh keep working.
    /usr/local/bin/jumpy
)

drop() {
    local privileged=$1 path=$2
    if [ ! -e "$path" ] && [ ! -L "$path" ]; then
        echo "  already gone: $path"
    elif [ "$apply" -eq 1 ]; then
        $privileged rm -rf -- "$path"
        echo "  removed: $path"
    else
        echo "  would remove: $path"
    fi
}

echo "== deployed symlinks from retired packages"
for path in "${USER_PATHS[@]}"; do drop "" "$path"; done

echo
echo "== files left in system paths (needs root)"
for path in "${SYSTEM_PATHS[@]}"; do drop sudo "$path"; done

echo
echo "== enablement broken by the geoclue -> rootfs-geoclue rename"
# The .wants symlink still points at the pre-rename repo path, so the timer
# is enabled only until the next daemon-reload. Deleting it would disable a
# working timer; reenable repoints it at the real unit file in /etc.
for unit in geo-timezone.timer geo-timezone.service geoclue-agent.service; do
    if ! systemctl cat "$unit" >/dev/null 2>&1; then
        echo "  not present: $unit"
    elif [ "$apply" -eq 1 ]; then
        sudo systemctl reenable "$unit" >/dev/null 2>&1 || true
        echo "  reenabled: $unit"
    else
        echo "  would reenable: $unit"
    fi
done

echo
echo "== user units that should no longer be enabled"
# waybar@sway: its package is gone, but .wants lives outside the package so
# the enablement survived. tailscale-systray and hyprwhspr moved into
# hyprland's autostart list, so leaving them enabled means two mechanisms
# start the same unit.
#
# NOTE: `systemctl --user disable` on a unit whose file is a stow symlink
# ("linked" state) removes the unit file too, not just the .wants link. Only
# the .wants link is wanted here, so remove that directly instead.
for unit in waybar@sway.service tailscale-systray.service hyprwhspr.service; do
    link=$(find "$HOME/.config/systemd/user" -path "*.wants/$unit" 2>/dev/null | head -1)
    if [ -z "$link" ]; then
        echo "  not enabled: $unit"
    elif [ "$apply" -eq 1 ]; then
        rm -- "$link"
        echo "  removed enablement: $link"
    else
        echo "  would remove enablement: $link"
    fi
done

echo
if [ "$apply" -eq 1 ]; then
    systemctl --user daemon-reload
    sudo systemctl daemon-reload
    echo "done. now run: task deploy:linux"
else
    echo "dry run: nothing changed. re-run with -y to apply."
fi
