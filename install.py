#!/usr/bin/env python3
"""Copy the files stow cannot symlink.

Stow owns every other package. Its symlinks are the point: an edit in the
repo is live with no redeploy, and a stale deployed copy is impossible. The
subtrees declared in COPIES are exceptions because their readers cannot
follow a link into /home -- the kernel reads modprobe.d at module load, and
the geoclue units run before /home is mounted.

Stdlib only, on a plain python3 shebang rather than the `uv run` trampoline
the other scripts use: this runs against a machine that is not provisioned
yet, so it must not need a venv of its own.
"""

from __future__ import annotations

import argparse
import logging
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent
DIR_MODE = "0755"
SPAWN_TIMEOUT = 300

log = logging.getLogger("install")


@dataclass(frozen=True)
class CopySpec:
    """One subtree copied to a system path instead of being symlinked.

    `stowed` says whether the source package is also passed to stow. When it
    is, the package's .stow-local-ignore must exclude this subtree or stow
    symlinks the file first and the copy then overwrites the symlink -- a
    silent, one-directional coupling that `check` exists to catch.
    """

    source: str
    dest: str
    pattern: str
    mode: str
    stowed: bool

    @property
    def package(self) -> str:
        return self.source.split("/", 1)[0]

    @property
    def relative(self) -> str:
        """Source path as stow sees it, with the package segment dropped."""
        return self.source.split("/", 1)[1]

    def files(self) -> list[Path]:
        return sorted((REPO / self.source).glob(self.pattern))


COPIES = (
    CopySpec(
        "rootfs/usr/local/share/wayland-sessions",
        "/usr/local/share/wayland-sessions",
        "*.desktop",
        "0644",
        stowed=True,
    ),
    CopySpec("rootfs/etc/modprobe.d", "/etc/modprobe.d", "*.conf", "0644", stowed=True),
    CopySpec(
        "geoclue/etc/geoclue/conf.d",
        "/etc/geoclue/conf.d",
        "*.conf",
        "0644",
        stowed=False,
    ),
    CopySpec(
        "geoclue/etc/systemd/system", "/etc/systemd/system", "*", "0644", stowed=False
    ),
    CopySpec(
        "geoclue/etc/NetworkManager/dispatcher.d",
        "/etc/NetworkManager/dispatcher.d",
        "09-timezone",
        "0755",
        stowed=False,
    ),
    CopySpec(
        "geoclue/usr/local/bin", "/usr/local/bin", "geo-timezone", "0755", stowed=False
    ),
)


def run(cmd: list[str], *, dry_run: bool) -> None:
    log.debug("spawn: %s", " ".join(cmd))
    if dry_run:
        return
    subprocess.run(
        cmd, check=True, stdout=sys.stderr, stderr=sys.stderr, timeout=SPAWN_TIMEOUT
    )


def ignore_gaps() -> list[str]:
    """Stowed copy sources whose package does not exclude them from stow.

    Approximates stow's matching: each .stow-local-ignore line is a Perl regex
    searched against the package-relative path. Close enough to catch the real
    mistake, which is forgetting the entry outright.
    """
    gaps = []
    for spec in COPIES:
        if not spec.stowed:
            continue
        ignore = REPO / spec.package / ".stow-local-ignore"
        patterns = ignore.read_text().split() if ignore.exists() else []
        if not any(re.search(p, spec.relative) for p in patterns):
            gaps.append(
                f"{spec.source} is stowed but no .stow-local-ignore entry excludes it"
            )
    return gaps


def cmd_check(args: argparse.Namespace) -> int:
    for spec in COPIES:
        found = spec.files()
        log.info(
            "%s -> %s (%d files, mode %s)",
            spec.source,
            spec.dest,
            len(found),
            spec.mode,
        )
        if not found:
            log.warning("  no files match %s", spec.pattern)

    gaps = ignore_gaps()
    for gap in gaps:
        log.error(gap)

    return 1 if gaps else 0


def cmd_system(args: argparse.Namespace) -> int:
    gaps = ignore_gaps()
    for gap in gaps:
        log.error(gap)
    if gaps:
        return 1

    for spec in COPIES:
        found = spec.files()
        if not found:
            log.warning("skipping %s: no files match %s", spec.source, spec.pattern)
            continue
        run(["sudo", "install", "-d", "-m", DIR_MODE, spec.dest], dry_run=args.dry_run)
        run(
            ["sudo", "install", "-m", spec.mode, *[str(f) for f in found], spec.dest],
            dry_run=args.dry_run,
        )
        log.info("installed %d file(s) into %s", len(found), spec.dest)

    run(["sudo", "systemctl", "daemon-reload"], dry_run=args.dry_run)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging.")
    parser.add_argument(
        "-n", "--dry-run", action="store_true", help="Do not change anything."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "check", help="Report planned work and verify stow coverage."
    ).set_defaults(func=cmd_check)
    sub.add_parser("system", help="Copy the system files. Needs sudo.").set_defaults(
        func=cmd_system
    )

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
