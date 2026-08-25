#!/usr/bin/env python3
"""Copy the files stow cannot symlink.

Stow owns every package under `~`. Its symlinks are the point there: an edit in
the repo is live with no redeploy, and a stale deployed copy is impossible. The
packages below target `/` instead, where that property is a liability -- a bare
`git checkout` would rewrite live system config -- and where several readers
cannot follow a link into /home at all: the kernel reads modprobe.d at module
load, and the geoclue units run before /home is mounted.

Two path groups get extra care. `rootfs/etc/sudoers.d` is validated with
`visudo` before anything is written and again after (see SUDOERS), and
`rootfs/etc/pam.d` is placed by an atomic rename rather than `install`, because
a half-written PAM service file denies every login (see ATOMIC).

Stdlib only, on a plain python3 shebang rather than the `uv run` trampoline the
other scripts use: this runs against machines that are not provisioned yet, so
it must not need a venv of its own.
"""

from __future__ import annotations

import argparse
import difflib
import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent
BACKUPS = Path(os.path.expanduser("~/.local/state/dotfiles/install"))
DIR_MODE = "0755"
MANIFEST = "manifest.json"
OWNER = "root:root"
SPAWN_TIMEOUT = 300

# Packages copied to `/` rather than stowed. Neither goes through stow at all,
# so there is no .stow-local-ignore to keep in sync -- getting one of those
# regexes subtly wrong is silent, and a bare directory name also matches a
# same-named file elsewhere in the package.
PACKAGES = ("rootfs", "rootfs-geoclue")

# Nothing is deferred any more; every tracked file under PACKAGES is installed.
# Kept as the lever for staging a future risky path in the same way pam.d and
# sudoers.d were staged out of the original stow migration.
DEFERRED: tuple[str, ...] = ()

# `install` is unlink-then-create (verified by strace), so a crash between the
# two leaves the path missing. For most files that is a transient nuisance. For
# a PAM service file it is not: /etc/pam.d/other is pam_deny + pam_warn on all
# four stanzas, so a missing service file denies every authentication attempt
# until someone repairs it by hand. These get a temp file plus an atomic
# rename(2) instead, which has no state where the path does not resolve.
ATOMIC = ("rootfs/etc/pam.d/",)

# sudo refuses to read a sudoers snippet it considers malformed, so a candidate
# is validated BEFORE it is written. `visudo -c -f` checks syntax only -- the
# owner/mode checks run only when no path argument is given -- so a run that
# touches these also re-runs a pathless `visudo -c` afterwards as its gate.
SUDOERS = ("rootfs/etc/sudoers.d/",)

# Git records only the executable bit, so it yields 0644 or 0755 and nothing
# else. Any path whose mode is load-bearing beyond that belongs here.
MODE_OVERRIDES = {"rootfs/etc/sudoers.d/clamav": "0440"}

log = logging.getLogger("install")


@dataclass(frozen=True)
class Entry:
    """One tracked file and where it lands on the system."""

    source: str
    dest: Path
    mode: str

    @property
    def package(self) -> str:
        return self.source.split("/", 1)[0]

    @property
    def relative(self) -> str:
        """Source path with the package segment dropped."""
        return self.source.split("/", 1)[1]


@dataclass
class Change:
    """Everything that differs between a tracked file and its system path."""

    entry: Entry
    kind: str
    link: str | None
    mode: str | None
    owner: str | None
    content: str

    @property
    def needed(self) -> bool:
        return bool(self.decisions)

    @property
    def decisions(self) -> list[str]:
        """What will actually be done to this path, in plain words."""
        if self.kind == "symlink":
            target = f"mode {self.entry.mode}, owner {OWNER}"
            return [f"replace the symlink with a regular file, {target}"]
        if self.kind == "missing":
            return [f"create the file, mode {self.entry.mode}, owner {OWNER}"]

        todo = []
        if self.content:
            todo.append("overwrite the content from the repo")
        if self.mode != self.entry.mode:
            todo.append(f"chmod {self.mode} -> {self.entry.mode}")
        if self.owner != OWNER:
            todo.append(f"chown {self.owner} -> {OWNER}")
        return todo


def git(*args: str) -> str:
    cmd = ["git", "-C", str(REPO), *args]
    log.info("spawn: %s", " ".join(cmd))
    done = subprocess.run(
        cmd, check=True, capture_output=True, text=True, timeout=SPAWN_TIMEOUT
    )
    if done.stderr.strip():
        log.info("git stderr: %s", done.stderr.strip())
    return done.stdout


def manifest() -> list[Entry]:
    """Every file this script installs, derived from git rather than a list.

    Three separate hand counts of `rootfs` disagreed; the index is the only
    source that cannot drift as files are added.
    """
    entries = []
    for line in git("ls-files", "-s", *PACKAGES).splitlines():
        git_mode, _, _, source = line.split(maxsplit=3)
        if source.startswith(DEFERRED):
            continue
        if git_mode not in ("100644", "100755"):
            # 120000 is a tracked symlink, 160000 a submodule. `install` would
            # dereference the first and choke on the second; neither belongs in
            # a system path, so fail loudly rather than install something odd.
            raise ValueError(f"{source}: unsupported git mode {git_mode}")
        mode = MODE_OVERRIDES.get(source, "0755" if git_mode == "100755" else "0644")
        entries.append(Entry(source, Path("/") / source.split("/", 1)[1], mode))
    return sorted(entries, key=lambda e: e.source)


def read(path: Path) -> str:
    """Current content of a system path, empty when it does not exist yet."""
    try:
        return path.read_text()
    except FileNotFoundError:
        return ""
    except PermissionError:
        done = subprocess.run(
            ["sudo", "-n", "cat", str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=SPAWN_TIMEOUT,
        )
        return done.stdout if done.returncode == 0 else ""


def record(change: Change, saved: str | None) -> dict[str, object]:
    """One backup manifest entry: what was there, and what replaced it.

    `previous` is what a restore has to put back; `saved` is null when there was
    nothing to save because the path did not exist.
    """
    entry = change.entry
    previous: dict[str, object] = {"kind": change.kind}
    if change.kind == "symlink":
        previous["link"] = change.link
    elif change.kind == "file":
        previous["mode"] = change.mode
        previous["owner"] = change.owner
    return {
        "dest": str(entry.dest),
        "source": entry.source,
        "saved": saved,
        "previous": previous,
        "installed": {"mode": entry.mode, "owner": OWNER},
        "decisions": change.decisions,
    }


def read_strict(path: Path) -> str:
    """Content of a system path, raising rather than yielding "" on failure.

    `read()` folds missing, empty and unreadable into the same empty string.
    That is harmless for a diff and destructive for a backup: an unreadable
    file would be saved as zero bytes and then overwritten, losing the only
    copy behind a backup that looks like it worked.
    """
    try:
        return path.read_text()
    except PermissionError:
        done = subprocess.run(
            ["sudo", "-n", "cat", str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=SPAWN_TIMEOUT,
        )
        if done.returncode != 0:
            raise PermissionError(f"cannot read {path} to back it up") from None
        return done.stdout


def attributes(path: Path) -> tuple[str, str] | None:
    """Mode and `owner:group` of a path, without following symlinks."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except PermissionError:
        done = subprocess.run(
            ["sudo", "-n", "stat", "-c", "%a %U:%G", str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=SPAWN_TIMEOUT,
        )
        if done.returncode != 0:
            return None
        mode, owner = done.stdout.split()
        return mode.zfill(4), owner
    from grp import getgrgid
    from pwd import getpwuid

    return (
        format(info.st_mode & 0o7777, "04o"),
        f"{getpwuid(info.st_uid).pw_name}:{getgrgid(info.st_gid).gr_name}",
    )


def inspect(entry: Entry) -> Change:
    """Compare one tracked file against its system path on every axis.

    Content equality alone is the wrong test: every path being taken over is a
    stow symlink INTO the repo, so it reads back byte-identical while still
    needing to become a real root-owned file. The symlink case is checked first
    because lstat on a symlink reports mode 0777, which means nothing.
    """
    if entry.dest.is_symlink():
        return Change(entry, "symlink", os.readlink(entry.dest), None, None, "")

    found = attributes(entry.dest)
    if found is None:
        return Change(entry, "missing", None, None, None, "")

    wanted = (REPO / entry.source).read_text()
    live = read(entry.dest)
    body = (
        ""
        if live == wanted
        else "".join(
            difflib.unified_diff(
                live.splitlines(keepends=True),
                wanted.splitlines(keepends=True),
                fromfile=f"{entry.dest} (live)",
                tofile=f"{entry.source} (repo)",
            )
        )
    )
    mode, owner = found
    return Change(entry, "file", None, mode, owner, body)


def report(change: Change) -> None:
    """Print every axis of one change, so nothing lands unexplained."""
    entry = change.entry
    log.info("%s", entry.dest)
    log.info("    source   %s", entry.source)
    if change.kind == "symlink":
        log.info("    state    symlink -> %s  =>  regular file", change.link)
        log.info("    mode     (symlink)  =>  %s", entry.mode)
        log.info("    owner    (symlink)  =>  %s", OWNER)
    elif change.kind == "missing":
        log.info("    state    absent  =>  regular file")
        log.info("    mode     -  =>  %s", entry.mode)
        log.info("    owner    -  =>  %s", OWNER)
    else:
        marker = "" if change.mode == entry.mode else "   CHANGES"
        log.info("    mode     %s  =>  %s%s", change.mode, entry.mode, marker)
        marker = "" if change.owner == OWNER else "   CHANGES"
        log.info("    owner    %s  =>  %s%s", change.owner, OWNER, marker)
    log.info(
        "    content  %s", "differs, diff below" if change.content else "identical"
    )
    for verdict in change.decisions or ["nothing to change"]:
        log.info("    DECISION %s", verdict)
    if change.content:
        sys.stderr.write(change.content)


@dataclass
class Directory:
    """A destination directory and what installing into it would do to it."""

    path: Path
    mode: str | None
    owner: str | None

    @property
    def exists(self) -> bool:
        return self.mode is not None

    @property
    def changes(self) -> bool:
        """`install -d -m` chmods a directory that already exists."""
        return bool(self.decisions)

    @property
    def decisions(self) -> list[str]:
        if not self.exists:
            return [f"create the directory, mode {DIR_MODE}, owner {OWNER}"]
        if self.mode != DIR_MODE:
            return [f"chmod {self.mode} -> {DIR_MODE}"]
        return []


def directories(changes: list[Change]) -> list[Directory]:
    """Every destination directory involved, including ancestors to create.

    Reported whether or not it needs work: `install -d -m 0755` runs against
    each one and will chmod an existing directory, so a mode that is about to
    change silently is exactly what this is here to surface.
    """
    seen: dict[Path, Directory] = {}
    for change in changes:
        path = change.entry.dest.parent
        while path not in seen:
            found = attributes(path)
            seen[path] = Directory(path, *(found or (None, None)))
            if found is not None or path == path.parent:
                break
            path = path.parent
    return sorted(seen.values(), key=lambda d: d.path)


def report_directories(dirs: list[Directory]) -> None:
    for entry in dirs:
        log.info("    %s", entry.path)
        if not entry.exists:
            log.info("        state    absent  =>  created")
            log.info("        mode     -  =>  %s", DIR_MODE)
            log.info("        owner    -  =>  %s", OWNER)
        else:
            marker = "   CHANGES" if entry.mode != DIR_MODE else ""
            log.info("        state    exists")
            log.info("        mode     %s  =>  %s%s", entry.mode, DIR_MODE, marker)
            log.info("        owner    %s (left alone)", entry.owner)
        for verdict in entry.decisions or ["nothing to change"]:
            log.info("        DECISION %s", verdict)


def preflight(pending: list[Change]) -> list[str]:
    """Validate every candidate before anything is written. Empty means go.

    Runs as one pass over the whole set, so a file that would be rejected stops
    the run with the system untouched rather than after N paths have already
    been replaced.
    """
    problems = []
    for change in pending:
        source = REPO / change.entry.source
        if not change.entry.source.startswith(SUDOERS):
            continue
        cmd = ["visudo", "-c", "-f", str(source)]
        log.info("spawn: %s", " ".join(cmd))
        done = subprocess.run(
            cmd, check=False, capture_output=True, text=True, timeout=SPAWN_TIMEOUT
        )
        if done.returncode != 0:
            # visudo reports the offending line on stderr, the summary on stdout
            detail = (done.stderr.strip() or done.stdout.strip()).replace("\n", "; ")
            problems.append(f"{change.entry.source}: {detail}")
        else:
            log.info("    sudoers syntax ok: %s", change.entry.source)
    return problems


def install_file(entry: Entry, *, atomic: bool) -> None:
    """Put one repo file at its system path with the declared mode and owner."""
    run(["sudo", "install", "-d", "-m", DIR_MODE, str(entry.dest.parent)])
    place = [
        "sudo",
        "install",
        "-m",
        entry.mode,
        "-o",
        "root",
        "-g",
        "root",
        str(REPO / entry.source),
    ]
    if not atomic:
        run([*place, str(entry.dest)])
        return

    # Same directory, so the rename stays on one filesystem.
    staged = entry.dest.with_name(f"{entry.dest.name}.installing")
    run([*place, str(staged)])
    run(["sudo", "mv", "-T", str(staged), str(entry.dest)])


def run(cmd: list[str], *, dry_run: bool = False) -> None:
    log.info("spawn: %s", " ".join(cmd))
    if dry_run:
        return
    subprocess.run(
        cmd, check=True, stdout=sys.stderr, stderr=sys.stderr, timeout=SPAWN_TIMEOUT
    )


def install(args: argparse.Namespace) -> int:
    changes = [inspect(e) for e in manifest()]
    pending = [c for c in changes if c.needed]
    log.info(
        "%d of %d managed file(s) need work; %d already correct",
        len(pending),
        len(changes),
        len(changes) - len(pending),
    )
    settled = [c for c in changes if not c.needed]
    if settled:
        log.info("%s", "-" * 60)
        log.info("already correct")
        for change in settled:
            log.info(
                "    %s (mode %s, owner %s)",
                change.entry.dest,
                change.mode,
                change.owner,
            )

    if not pending:
        return 0

    dirs = directories(pending)
    altered = [d for d in dirs if d.changes]
    log.info("%d destination directories, %d of which change", len(dirs), len(altered))
    log.info("%s", "-" * 60)
    log.info("directories")
    report_directories(dirs)

    log.info("%s", "-" * 60)
    log.info("files")
    for change in pending:
        report(change)

    log.info("%s", "-" * 60)
    log.info("preflight")
    problems = preflight(pending)
    for problem in problems:
        log.error("%s", problem)
    if problems:
        log.error("refusing to install anything; nothing was changed")
        return 1
    log.info("    all candidates validated")

    if args.dry_run:
        log.info("%s", "-" * 60)
        log.info("dry run: nothing was changed")
        return 0

    stamp = datetime.now(UTC)
    backup = BACKUPS / stamp.strftime("%Y%m%d-%H%M%S")
    log.info("%s", "-" * 60)
    log.info("backing up every replaced file under %s", backup)
    log.info("(said before the first write, so a mid-run failure still names it)")

    # Back everything up BEFORE installing anything, so a file that cannot be
    # read aborts the run with the system untouched. Interleaving the two would
    # leave the first N paths already replaced when the N+1th fails -- stow
    # plans every package and then aborts wholesale, and this keeps that.
    saved_entries = []
    for change in pending:
        entry = change.entry
        if change.kind == "missing":
            saved_entries.append(record(change, None))
            continue
        saved = backup / entry.relative
        saved.parent.mkdir(parents=True, exist_ok=True)
        saved.write_text(read_strict(entry.dest))
        saved_entries.append(record(change, entry.relative))
        log.info("backed up %s to %s", entry.dest, saved)

    notes = backup / MANIFEST
    notes.parent.mkdir(parents=True, exist_ok=True)
    notes.write_text(
        json.dumps(
            {
                "created": stamp.isoformat(timespec="seconds"),
                "repo": str(REPO),
                "backup": str(backup),
                "entries": saved_entries,
            },
            indent=2,
        )
        + "\n"
    )
    log.info("%s", "-" * 60)
    log.info("every replaced file is backed up; wrote %s", notes)
    log.info("installing")

    touched_sudoers = False
    for change in pending:
        entry = change.entry
        atomic = entry.source.startswith(ATOMIC)
        install_file(entry, atomic=atomic)
        touched_sudoers = touched_sudoers or entry.source.startswith(SUDOERS)
        log.info("installed %s%s", entry.dest, " (atomic)" if atomic else "")

    if touched_sudoers:
        log.info("%s", "-" * 60)
        log.info("verifying sudoers as installed (this is the owner/mode gate)")
        cmd = ["sudo", "visudo", "-c"]
        log.info("spawn: %s", " ".join(cmd))
        done = subprocess.run(
            cmd, check=False, capture_output=True, text=True, timeout=SPAWN_TIMEOUT
        )
        for line in done.stdout.splitlines():
            log.info("    %s", line)
        if done.returncode != 0:
            log.error(
                "sudoers is NOT valid; restore from %s before logging out", backup
            )
            return 1

    log.info("%s", "-" * 60)
    log.info("replaced files backed up under %s", backup)
    log.info("what each path was before is in %s", notes)
    log.info("restore one: read its entry there, then")
    log.info("  sudo cp %s/<saved> <dest>", backup)
    log.info(
        "  sudo chmod <previous.mode> <dest> && sudo chown <previous.owner> <dest>"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Show the diffs and change nothing.",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    return install(args)


if __name__ == "__main__":
    sys.exit(main())
