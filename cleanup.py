#!/usr/bin/env -S sh -c 'd="$(dirname "$0")"; uv sync -q --project "$d" && exec "$d/.venv/bin/python" "$0" "$@"'
# vim: ft=python
"""Remove what past migrations left behind on this machine.

The 2026-08 migration retired the macOS target and the sway stack, and moved
rootfs/ off stow onto install.py. Neither stow nor install.py deletes
anything, so every machine that deployed the old layout still carries the
leftovers listed below. Run it on each machine after pulling; it has grown
past a single migration, so re-run it when this file changes rather than
deleting it after one pass.

Everything here is idempotent: an entry that is already gone is skipped.

    ./cleanup.py        show what would happen
    ./cleanup.py -y     do it

Stdlib only, on the root project the trampoline syncs (no dependencies), so
the venv it creates is empty.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent
HOME = Path.home()
# Stow links into the main checkout, which is not REPO when this runs from a
# worktree.
REPOS = (REPO, HOME / ".dotfiles")
USER_UNITS = HOME / ".config/systemd/user"
FONTS = HOME / ".local/share/fonts"

log = logging.getLogger("cleanup")


@dataclass(frozen=True)
class Entry:
    path: Path
    # A safe entry is removed only while it is stow residue: a dangling link,
    # a link into this repo, or a directory holding nothing else. A real file
    # that shares the name is kept.
    safe: bool


def unconditional(*paths: str) -> list[Entry]:
    return [Entry(Path(p).expanduser(), False) for p in paths]


def stale(*paths: str) -> list[Entry]:
    return [Entry(Path(p).expanduser(), True) for p in paths]


# Deployed symlinks whose package no longer exists in the repo.
USER_PATHS = [
    # sway and its helpers
    *unconditional(
        "~/.config/sway",
        "~/.config/swaylock",
        "~/.config/swayr",
        "~/.config/sworkstyle",
        "~/.config/pacman/sway.txt",
        "~/.config/uwsm/env-sway",
        "~/.config/waybar/sway.jsonc",
        "~/.config/xdg-desktop-portal/sway-portals.conf",
        "~/.config/systemd/user/autotiling-rs.service",
        "~/.config/systemd/user/swayidle.service",
        "~/.config/systemd/user/swayrd.service",
        "~/.config/systemd/user/sworkstyle.service",
        "~/.config/systemd/user/wl-gammarelay-rs.service",
        "~/.config/systemd/user/waybar@sway.service.d",
    ),
    # retired earlier in the same migration. NOT ~/.config/google-chrome --
    # the repo's symlinks under it are already gone, but the directory holds
    # native-messaging manifests written by Bitwarden, Claude and Granted.
    *unconditional(
        "~/.config/chrome-flags.conf",
        "~/.config/systemd/user/dex.service",
    ),
    # mako, replaced by swaync
    *unconditional("~/.config/mako"),
    # lib modules that moved into the shared dotlib package. stow never removes
    # a link whose source left the repo, so these dangle after the migration.
    # notify.py already dangles, from commit 035fae7.
    *unconditional(
        "~/.config/hypr/scripts/lib/cli.py",
        "~/.config/hypr/scripts/lib/notify.py",
        "~/.config/wayland/scripts/lib/cli.py",
        "~/.config/wayland/scripts/lib/desktop.py",
        "~/.config/wayland/scripts/lib/notify.py",
        "~/.local/bin/lib",
    ),
    # hyprctl briefly lived in dotlib before moving back to the hyprland
    # project, so a machine that deployed in between has this dangling.
    *unconditional("~/.config/wayland/lib/src/dotlib/hyprctl.py"),
    # Older removals whose deployed links were never cleaned. ai.lua went in
    # bd349de, speech.md in a208651; config.yaml.new was never tracked at all,
    # so it is a stray a hyprpilot run left behind.
    *unconditional(
        "~/.config/hypr/modes/ai.lua",
        "~/.config/wayland/scripts/speech.md",
        "~/.config/hyprpilot/config.yaml.new",
    ),
    # the kitty platform split collapsed to a single globinclude
    *unconditional(
        "~/.config/kitty/linux.conf",
        "~/.config/kitty/macos.conf",
    ),
    # hyprwhspr, replaced by speech.py's own capture. Its unit file and state
    # are real files its installer wrote, not stow links, so removing the
    # package leaves them. Uninstall `hyprwhspr` from the AUR separately.
    *unconditional(
        "~/.config/hyprwhspr",
        "~/.config/systemd/user/hyprwhspr.service",
        "~/.config/systemd/user/hyprwhspr.service.d",
        "~/.local/share/hyprwhspr",
        "~/.local/state/hyprwhspr",
    ),
    # playerctl-waybar, replaced by waybar's native mpris module. It only
    # poked RTMIN+5 on spotify metadata changes; mpris is D-Bus driven.
    *unconditional("~/.config/systemd/user/playerctl-waybar.service"),
    # Links whose repo sources were removed after the migration, found
    # dangling on emanet 2026-10-29. Not removed unconditionally: some of
    # these names are ones an app may write itself.
    #
    # the sway session units and the audio idle inhibitor
    *stale(
        "~/.config/systemd/user/sway-session.service",
        "~/.config/systemd/user/sway-session.target",
        "~/.config/systemd/user/sway-audio-idle-inhibit.service",
    ),
    # hyprland's session units, replaced by uwsm, and the drop-ins of the
    # ydotool and hyprpilot units that no longer ship
    *stale(
        "~/.config/systemd/user/hyprland-session.service",
        "~/.config/systemd/user/hyprland-session.target",
        "~/.config/systemd/user/ydotool.service.d",
        "~/.config/systemd/user/hyprpilot.service.d",
    ),
    # the termfilechooser scripts and the opencode tui/notify files, now
    # carried by other packages or dropped
    *stale(
        "~/.config/xdg-desktop-portal-termfilechooser/ranger.sh",
        "~/.config/xdg-desktop-portal-termfilechooser/common.sh",
        "~/.config/opencode-kilic/tui.json",
        "~/.config/opencode-kilic/plugins/notify.js",
        "~/.config/opencode-laravel/tui.json",
        "~/.config/opencode-laravel/plugins/notify.js",
    ),
    # the wayland lib modules that moved into dotlib along with the rest
    *stale(
        "~/.config/wayland/scripts/lib/enrich.py",
        "~/.config/wayland/scripts/lib/waybar.py",
    ),
    # a DaVinci Resolve launcher and the Consolas ligature font, both dropped
    *stale(
        "~/.local/share/applications/DaVinciResolve.desktop",
        "~/.local/share/fonts/TTF/Consolasligaturizedv3.ttf",
        "~/.local/share/fonts/TTF/Consolasligaturizedv3-Bold.ttf",
        "~/.local/share/fonts/TTF/Consolasligaturizedv3-Italic.ttf",
        "~/.local/share/fonts/TTF/Consolasligaturizedv3-BoldItalic.ttf",
    ),
    # a stow run from the wrong directory linked playerctl-waybar.service
    # under ~/systemd instead of ~/.config/systemd/user
    *stale("~/systemd"),
]

# Real files install.py had already copied, so removing the source left them
# behind. The sway entries still show sway in the greeter, pointing at a
# binary that is no longer installed.
SYSTEM_PATHS = [
    *unconditional(
        "/usr/local/share/wayland-sessions/sway-amd-uwsm.desktop",
        "/usr/local/share/wayland-sessions/sway-nvidia-uwsm.desktop",
        "/etc/systemd/system/sysctl-dotfiles.service",
        "/etc/tlp.conf.pacsave",
        "/usr/local/bin/__pycache__",
        # jumpy moved to ~/.local/bin. Remove ONLY after home-assistant!69
        # merges -- until then this is what makes the old bare `jumpy` over
        # ssh keep working.
        "/usr/local/bin/jumpy",
    ),
    # links from the stow-based rootfs, dangling since it moved to install.py
    *stale(
        "/etc/systemd/user/sway-session.service",
        "/etc/systemd/user/sway-session.target",
        "/etc/udev/scripts/wayland-notify-send.sh",
    ),
]

# NetworkManager owns the interfaces on hosts that enable it, so networkd's
# per-interface files for them are stale as well.
NETWORKD_UNITS = (
    "systemd-networkd.service",
    "systemd-networkd.socket",
    "systemd-networkd-wait-online.service",
)
NETWORKD_FILES = [
    f"/etc/systemd/network/20-{name}.network" for name in ("ethernet", "wlan", "wwan")
]


class Cleaner:
    def __init__(self, apply: bool) -> None:
        self.apply = apply
        self.fonts_removed = False

    def run(
        self, *cmd: str, privileged: bool = False, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        argv = ["sudo", *cmd] if privileged else list(cmd)
        return subprocess.run(
            argv, check=check, capture_output=True, text=True, timeout=120
        )

    def systemctl(self, *args: str) -> subprocess.CompletedProcess[str]:
        return self.run("systemctl", *args, check=False)

    def into_repo(self, link: Path) -> bool:
        target = Path(os.path.normpath(link.parent / os.readlink(link)))
        return any(target.is_relative_to(repo) for repo in REPOS)

    def residue(self, path: Path) -> bool:
        if path.is_symlink():
            return not path.exists() or self.into_repo(path)
        if path.is_dir():
            return all(self.residue(child) for child in path.iterdir())

        return False

    def drop(self, entry: Entry, privileged: bool) -> None:
        path = entry.path
        if not path.exists() and not path.is_symlink():
            log.info("  already gone: %s", path)
        elif entry.safe and not self.residue(path):
            log.info("  kept, not stow residue: %s", path)
        elif self.apply:
            self.run("rm", "-rf", "--", str(path), privileged=privileged)
            self.fonts_removed |= path.is_relative_to(FONTS)
            log.info("  removed: %s", path)
        else:
            log.info("  would remove: %s", path)

    def drop_all(self, entries: list[Entry], privileged: bool) -> None:
        for entry in entries:
            self.drop(entry, privileged)

    def reenable_geoclue(self) -> None:
        # The .wants symlink still points at the pre-rename repo path, so the
        # timer is enabled only until the next daemon-reload. Deleting it
        # would disable a working timer; reenable repoints it at the real
        # unit file in /etc.
        for unit in (
            "geo-timezone.timer",
            "geo-timezone.service",
            "geoclue-agent.service",
        ):
            if self.systemctl("cat", unit).returncode != 0:
                log.info("  not present: %s", unit)
            elif self.apply:
                self.run("systemctl", "reenable", unit, privileged=True, check=False)
                log.info("  reenabled: %s", unit)
            else:
                log.info("  would reenable: %s", unit)

    def drop_enablement(self) -> None:
        # waybar@sway and hyprwhspr: their packages are gone, but .wants
        # lives outside the package so the enablement survived.
        # tailscale-systray moved into hyprland's autostart list, so leaving
        # it enabled means two mechanisms start the same unit. hyprpilot was
        # removed.
        #
        # NOTE: `systemctl --user disable` on a unit whose file is a stow
        # symlink ("linked" state) removes the unit file too, not just the
        # .wants link. Only the .wants link is wanted here, so remove that
        # directly instead.
        for unit in (
            "waybar@sway.service",
            "tailscale-systray.service",
            "hyprwhspr.service",
            "hyprpilot.service",
        ):
            links = [
                Path(root) / unit
                for root, dirs, files in os.walk(USER_UNITS)
                if root.endswith(".wants") and unit in (*dirs, *files)
            ]
            if not links:
                log.info("  not enabled: %s", unit)
            for link in links:
                if self.apply:
                    link.unlink()
                    log.info("  removed enablement: %s", link)
                else:
                    log.info("  would remove enablement: %s", link)

    def networkd(self) -> None:
        # With NetworkManager enabled, systemd-networkd-wait-online waits for
        # interfaces networkd does not manage and times out after 2 minutes,
        # which holds up boot through docker.service -> network-online.target.
        if self.systemctl("is-enabled", "NetworkManager.service").returncode != 0:
            log.info("  NetworkManager is not enabled, leaving networkd alone")
            return

        off = []
        for unit in NETWORKD_UNITS:
            state = self.systemctl("is-enabled", unit).stdout.strip()
            active = self.systemctl("is-active", unit).returncode == 0
            if state in ("disabled", "masked") and not active:
                log.info("  already disabled: %s", unit)
            else:
                off.append(unit)
        if off and self.apply:
            self.run("systemctl", "disable", "--now", *off, privileged=True)
            log.info("  disabled: %s", " ".join(off))
        elif off:
            log.info("  would disable: %s", " ".join(off))

        wait = NETWORKD_UNITS[2]
        if self.systemctl("is-enabled", wait).stdout.strip() == "masked":
            log.info("  already masked: %s", wait)
        elif self.apply:
            self.run("systemctl", "mask", wait, privileged=True)
            log.info("  masked: %s", wait)
        else:
            log.info("  would mask: %s", wait)

        # networkd is off by now, applied or planned.
        self.drop_all([Entry(Path(p), False) for p in NETWORKD_FILES], True)

    def run_all(self) -> None:
        log.info("== deployed symlinks from retired packages")
        self.drop_all(USER_PATHS, False)

        log.info("\n== files left in system paths (needs root)")
        self.drop_all(SYSTEM_PATHS, True)

        log.info("\n== enablement broken by the geoclue -> rootfs-geoclue rename")
        self.reenable_geoclue()

        log.info("\n== user units that should no longer be enabled")
        self.drop_enablement()

        log.info("\n== systemd-networkd on a NetworkManager host")
        self.networkd()

        log.info("")
        if not self.apply:
            log.info("dry run: nothing changed. re-run with -y to apply.")
            return

        self.run("systemctl", "--user", "daemon-reload")
        self.run("systemctl", "daemon-reload", privileged=True)
        if self.fonts_removed:
            self.run("fc-cache", "-f")
        log.info("done. now run: task deploy:linux")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Apply the changes. Default: show them and change nothing.",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    Cleaner(args.yes).run_all()

    return 0


if __name__ == "__main__":
    sys.exit(main())
