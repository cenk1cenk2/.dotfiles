#!/usr/bin/env python3
"""Install a package's tracked files as real files, where stow would link them.

Stow owns every package under `~`, and its symlinks are the point there: an
edit in the repo is live with no redeploy, and a stale deployed copy is
impossible. Some packages cannot have that property. `rootfs` targets `/`,
where a bare `git checkout` would rewrite live system config and where several
readers cannot follow a link into /home at all -- the kernel reads modprobe.d
at module load, and the geoclue units run before /home is mounted. `obs`
targets `~` but saves its config by writing a temp file and renaming it over
the target, which replaces a symlink with a real file on the first save.

A package opts in by carrying a `.install.json` at its root. That file, not
this script, says where the package lands, who owns it, whether writing needs
sudo, which paths need special handling, and what to run afterwards. Nothing
about a specific package is hardcoded here -- only the strategies those rules
name are.

    ./install.py                every package that has a .install.json
    ./install.py obs rootfs     only these
    ./install.py -n             diff everything, change nothing

Two path groups get extra care, both declared by `rootfs/.install.json` rather
than assumed here. `etc/sudoers.d` is validated with `visudo` before anything
is written and again after (`validate: sudoers`), and `etc/pam.d` is placed by
an atomic rename rather than `install` (`write: atomic`), because a
half-written PAM service file denies every login.

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
from grp import getgrgid
from pathlib import Path
from pwd import getpwuid

REPO = Path(__file__).resolve().parent
BACKUPS = Path(os.path.expanduser("~/.local/state/dotfiles/install"))
CONFIG = ".install.json"
MANIFEST = "manifest.json"
SPAWN_TIMEOUT = 300

# Every key `.install.json` may carry. Unknown keys are rejected rather than
# ignored: this file decides whether a sudoers snippet gets validated, and a
# silently-dropped typo is exactly the failure that would not be noticed.
KEYS = frozenset(
    ("target", "owner", "sudo", "dir_mode", "modes", "ignore", "rules", "post")
)
RULE_KEYS = frozenset(("paths", "write", "validate"))

# What a rule's `write` may name. "install" is the default: `install(1)`, which
# is unlink-then-create (verified by strace), so a crash between the two leaves
# the path missing. For most files that is a transient nuisance. For a PAM
# service file it is not: /etc/pam.d/other is pam_deny + pam_warn on all four
# stanzas, so a missing service file denies every authentication attempt until
# someone repairs it by hand. "atomic" stages a temp file in the same directory
# and rename(2)s it over the target, which has no state where the path does not
# resolve.
WRITERS = ("install", "atomic")

# What a rule's `validate` may name. "sudoers": sudo refuses to read a snippet
# it considers malformed, so a candidate is checked BEFORE it is written.
# `visudo -c -f` checks syntax only -- the owner and mode checks run only when
# no path argument is given -- so a run that touches these also re-runs a
# pathless `visudo -c` afterwards as its gate.
VALIDATORS = ("sudoers",)

# Safety floors keyed on the DESTINATION path rather than on a package. "A file
# landing in /etc/sudoers.d must be syntax-checked" is a property of sudo, and
# "a file landing in /etc/pam.d must be placed atomically" a property of PAM;
# neither is a property of whichever package happens to carry the file. Before
# this script read config, both were hardcoded and so could not be forgotten.
# Config may make a rule stricter; it can never drop a floor.
FLOORS = (
    ("/etc/sudoers.d/", None, "sudoers"),
    ("/etc/pam.d/", "atomic", None),
)

log = logging.getLogger("install")


def me() -> str:
    """`user:group` of whoever is running this, the default owner."""
    return f"{getpwuid(os.getuid()).pw_name}:{getgrgid(os.getgid()).gr_name}"


@dataclass(frozen=True)
class Rule:
    """A set of path prefixes inside a package and how their files are handled."""

    paths: tuple[str, ...]
    write: str
    validate: str | None


@dataclass(frozen=True)
class Package:
    """One `.install.json` -- everything this script knows about a package."""

    name: str
    target: Path
    owner: str
    sudo: bool
    dir_mode: str
    modes: dict[str, str]
    ignore: tuple[str, ...]
    rules: tuple[Rule, ...]
    post: tuple[tuple[str, ...], ...]

    def rule(self, relative: str) -> Rule | None:
        """The first rule matching a package-relative path, if any."""
        for rule in self.rules:
            if relative.startswith(rule.paths):
                return rule
        return None

    def privileged(self, cmd: list[str]) -> list[str]:
        return ["sudo", *cmd] if self.sudo else cmd


@dataclass(frozen=True)
class Entry:
    """One tracked file and where it lands on the system."""

    package: Package
    relative: str
    mode: str

    @property
    def source(self) -> str:
        """Path as git spells it, package segment included."""
        return f"{self.package.name}/{self.relative}"

    @property
    def dest(self) -> Path:
        return self.package.target / self.relative

    @property
    def rule(self) -> Rule | None:
        """How this file is written and validated: config, plus any floor.

        None means the ordinary path -- `install`, no validation.
        """
        configured = self.package.rule(self.relative)
        write = configured.write if configured else "install"
        validate = configured.validate if configured else None

        dest = str(self.dest)
        for prefix, floor_write, floor_validate in FLOORS:
            if dest.startswith(prefix):
                write = floor_write or write
                validate = floor_validate or validate

        if write == "install" and validate is None:
            return None
        return Rule((), write, validate)


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
        entry = self.entry
        owner = entry.package.owner
        if self.kind == "symlink":
            target = f"mode {entry.mode}, owner {owner}"
            return [f"replace the symlink with a regular file, {target}"]
        if self.kind == "missing":
            return [f"create the file, mode {entry.mode}, owner {owner}"]

        todo = []
        if self.content:
            todo.append("overwrite the content from the repo")
        if self.mode != entry.mode:
            todo.append(f"chmod {self.mode} -> {entry.mode}")
        if self.owner != owner:
            todo.append(f"chown {self.owner} -> {owner}")
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


def typed(name: str, raw: dict, key: str, kind: type, default: object) -> object:
    """One config value, or a clean error naming the type that was expected.

    JSON makes a string and a list of strings easy to confuse, and the failure
    is silent rather than loud: `tuple("etc/pam.d/")` is ten one-character
    prefixes, so a rule written as a bare string matches every path beginning
    with any of `e t c / p a m . d` instead of the directory it names.
    """
    value = raw.get(key, default)
    if not isinstance(value, kind) or (kind is not bool and isinstance(value, bool)):
        raise SystemExit(
            f"{name}/{CONFIG}: {key} must be {kind.__name__}, "
            f"got {type(value).__name__} ({value!r})"
        )
    return value


def strings(name: str, key: str, value: list) -> tuple[str, ...]:
    """A config list that must hold only non-empty strings."""
    for item in value:
        if not isinstance(item, str) or not item:
            raise SystemExit(
                f"{name}/{CONFIG}: every {key} entry must be a non-empty string, "
                f"got {item!r}"
            )
    return tuple(value)


def rule_of(name: str, raw: object) -> Rule:
    """One `rules` entry, rejecting anything this script cannot honour."""
    if not isinstance(raw, dict):
        raise SystemExit(f"{name}/{CONFIG}: every rule must be an object")
    unknown = sorted(set(raw) - RULE_KEYS)
    if unknown:
        raise SystemExit(f"{name}/{CONFIG}: unknown rule key(s) {', '.join(unknown)}")

    paths = strings(name, "rules[].paths", typed(name, raw, "paths", list, []))
    if not paths:
        raise SystemExit(f"{name}/{CONFIG}: a rule needs a non-empty 'paths'")

    write = raw.get("write", "install")
    if write not in WRITERS:
        raise SystemExit(
            f"{name}/{CONFIG}: unknown write strategy {write!r}, "
            f"expected one of {', '.join(WRITERS)}"
        )

    validate = raw.get("validate")
    if validate is not None and validate not in VALIDATORS:
        raise SystemExit(
            f"{name}/{CONFIG}: unknown validate strategy {validate!r}, "
            f"expected one of {', '.join(VALIDATORS)}"
        )
    return Rule(paths, write, validate)


def load(name: str) -> Package:
    """Read one package's `.install.json`, or fail with what is wrong with it."""
    path = REPO / name / CONFIG
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        raise SystemExit(
            f"{name}: no {CONFIG}, so this script does not install it"
        ) from None
    except json.JSONDecodeError as err:
        raise SystemExit(f"{name}/{CONFIG}: {err}") from None

    if not isinstance(raw, dict):
        raise SystemExit(f"{name}/{CONFIG}: must be an object")
    unknown = sorted(set(raw) - KEYS)
    if unknown:
        raise SystemExit(f"{name}/{CONFIG}: unknown key(s) {', '.join(unknown)}")

    sudo = typed(name, raw, "sudo", bool, False)
    # A package that writes as root owns its files as root. Defaulting to the
    # invoking user would drop cenk-owned files into /etc, and sudo refuses to
    # read a sudoers snippet it does not own -- that alone locks you out.
    owner = typed(name, raw, "owner", str, "root:root" if sudo else me())
    if owner.count(":") != 1 or not all(owner.split(":")):
        raise SystemExit(f"{name}/{CONFIG}: owner must be 'user:group', got {owner!r}")

    modes = typed(name, raw, "modes", dict, {})
    for key, value in modes.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise SystemExit(
                f"{name}/{CONFIG}: modes maps a path string to a mode string, "
                f"got {key!r}: {value!r}"
            )

    post = []
    for cmd in typed(name, raw, "post", list, []):
        if not isinstance(cmd, list) or not cmd:
            raise SystemExit(
                f"{name}/{CONFIG}: every post entry is a non-empty list of "
                f"argv strings, got {cmd!r}"
            )
        post.append(strings(name, "post[]", cmd))

    return Package(
        name=name,
        target=Path(os.path.expanduser(typed(name, raw, "target", str, "~"))),
        owner=owner,
        sudo=sudo,
        dir_mode=typed(name, raw, "dir_mode", str, "0755"),
        modes=dict(modes),
        ignore=strings(name, "ignore", typed(name, raw, "ignore", list, [])),
        rules=tuple(
            rule_of(name, rule) for rule in typed(name, raw, "rules", list, [])
        ),
        post=tuple(post),
    )


def installable() -> list[str]:
    """Every package carrying a `.install.json`, in repo order."""
    return sorted(
        path.parent.name for path in REPO.glob(f"*/{CONFIG}") if path.parent.is_dir()
    )


def manifest(package: Package) -> list[Entry]:
    """Every file this script installs for a package, derived from git.

    Three separate hand counts of `rootfs` disagreed; the index is the only
    source that cannot drift as files are added.
    """
    entries = []
    tracked: set[str] = set()
    for line in git("ls-files", "-s", package.name).splitlines():
        git_mode, _, _, source = line.split(maxsplit=3)
        relative = source.split("/", 1)[1]
        if relative == CONFIG:
            continue
        tracked.add(relative)
        if relative.startswith(package.ignore):
            continue
        if git_mode not in ("100644", "100755"):
            # 120000 is a tracked symlink, 160000 a submodule. `install` would
            # dereference the first and choke on the second; neither belongs in
            # a destination path, so fail loudly rather than install something
            # odd.
            raise ValueError(f"{source}: unsupported git mode {git_mode}")
        # Git records only the executable bit, so it yields 0644 or 0755 and
        # nothing else. Any path whose mode is load-bearing beyond that needs a
        # `modes` entry in the package config.
        mode = package.modes.get(relative, "0755" if git_mode == "100755" else "0644")
        entries.append(Entry(package, relative, mode))

    # A configured path that matches no tracked file does nothing, and says
    # nothing. The likely mistake is the old package-prefixed spelling
    # ("rootfs/etc/sudoers.d/clamav"), which would quietly install that file
    # 0644 instead of 0440 and skip the rule that validates it.
    inert = [f"modes[{key!r}]" for key in package.modes if key not in tracked]
    inert += [
        f"ignore[{prefix!r}]"
        for prefix in package.ignore
        if not any(name.startswith(prefix) for name in tracked)
    ]
    inert += [
        f"rules[].paths[{prefix!r}]"
        for rule in package.rules
        for prefix in rule.paths
        if not any(name.startswith(prefix) for name in tracked)
    ]
    if inert:
        raise SystemExit(
            f"{package.name}/{CONFIG}: matches no tracked file: {', '.join(inert)}"
        )
    return sorted(entries, key=lambda e: e.relative)


def read(path: Path) -> bytes:
    """Current content of a destination path, empty when it does not exist.

    Bytes, not text: a package may carry images (opendeck's button icons) or
    anything else that is not UTF-8, and a comparison that decodes first would
    raise on the first PNG rather than report a difference.
    """
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return b""
    except PermissionError:
        done = subprocess.run(
            ["sudo", "-n", "cat", str(path)],
            check=False,
            capture_output=True,
            timeout=SPAWN_TIMEOUT,
        )
        return done.stdout if done.returncode == 0 else b""


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
        "package": entry.package.name,
        "saved": saved,
        "previous": previous,
        "installed": {"mode": entry.mode, "owner": entry.package.owner},
        "decisions": change.decisions,
    }


def read_strict(path: Path) -> bytes:
    """Content of a destination path, raising rather than yielding "" on failure.

    `read()` folds missing, empty and unreadable into the same empty result.
    That is harmless for a diff and destructive for a backup: an unreadable
    file would be saved as zero bytes and then overwritten, losing the only
    copy behind a backup that looks like it worked.
    """
    try:
        return path.read_bytes()
    except PermissionError:
        done = subprocess.run(
            ["sudo", "-n", "cat", str(path)],
            check=False,
            capture_output=True,
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

    return (
        format(info.st_mode & 0o7777, "04o"),
        f"{getpwuid(info.st_uid).pw_name}:{getgrgid(info.st_gid).gr_name}",
    )


def difference(entry: Entry, live: bytes, wanted: bytes) -> str:
    """A readable account of how two byte strings differ. Empty means equal.

    A unified diff where both sides are text, and a one-line summary where
    either side is not -- an image that changed still has to be reported, and
    dumping it to a terminal is worse than saying so.
    """
    if live == wanted:
        return ""
    try:
        before = live.decode().splitlines(keepends=True)
        after = wanted.decode().splitlines(keepends=True)
    except UnicodeDecodeError:
        return (
            f"binary content differs: {len(live)} bytes live, "
            f"{len(wanted)} bytes in {entry.source}\n"
        )
    return "".join(
        difflib.unified_diff(
            before,
            after,
            fromfile=f"{entry.dest} (live)",
            tofile=f"{entry.source} (repo)",
        )
    )


def inspect(entry: Entry) -> Change:
    """Compare one tracked file against its destination on every axis.

    Content equality alone is the wrong test: a path being taken over from stow
    is a symlink INTO the repo, so it reads back byte-identical while still
    needing to become a real file. The symlink case is checked first because
    lstat on a symlink reports mode 0777, which means nothing.
    """
    if entry.dest.is_symlink():
        return Change(entry, "symlink", os.readlink(entry.dest), None, None, "")

    found = attributes(entry.dest)
    if found is None:
        return Change(entry, "missing", None, None, None, "")

    wanted = (REPO / entry.source).read_bytes()
    live = read(entry.dest)
    mode, owner = found
    return Change(entry, "file", None, mode, owner, difference(entry, live, wanted))


def report(change: Change) -> None:
    """Print every axis of one change, so nothing lands unexplained."""
    entry = change.entry
    owner = entry.package.owner
    log.info("%s", entry.dest)
    log.info("    source   %s", entry.source)
    rule = entry.rule
    if rule is not None:
        detail = f"write {rule.write}"
        if rule.validate:
            detail += f", validate {rule.validate}"
        log.info("    rule     %s", detail)
    if change.kind == "symlink":
        log.info("    state    symlink -> %s  =>  regular file", change.link)
        log.info("    mode     (symlink)  =>  %s", entry.mode)
        log.info("    owner    (symlink)  =>  %s", owner)
    elif change.kind == "missing":
        log.info("    state    absent  =>  regular file")
        log.info("    mode     -  =>  %s", entry.mode)
        log.info("    owner    -  =>  %s", owner)
    else:
        marker = "" if change.mode == entry.mode else "   CHANGES"
        log.info("    mode     %s  =>  %s%s", change.mode, entry.mode, marker)
        marker = "" if change.owner == owner else "   CHANGES"
        log.info("    owner    %s  =>  %s%s", change.owner, owner, marker)
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

    package: Package
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
        wanted = self.package.dir_mode
        if not self.exists:
            return [f"create the directory, mode {wanted}, owner {self.package.owner}"]
        if self.mode != wanted:
            return [f"chmod {self.mode} -> {wanted}"]
        return []


def directories(changes: list[Change]) -> list[Directory]:
    """Every destination directory involved, including ancestors to create.

    Reported whether or not it needs work: `install -d -m` runs against each one
    and will chmod an existing directory, so a mode that is about to change
    silently is exactly what this is here to surface.
    """
    seen: dict[Path, Directory] = {}
    for change in changes:
        path = change.entry.dest.parent
        while path not in seen:
            found = attributes(path)
            seen[path] = Directory(change.entry.package, path, *(found or (None, None)))
            if found is not None or path == path.parent:
                break
            path = path.parent
    return sorted(seen.values(), key=lambda d: d.path)


def report_directories(dirs: list[Directory]) -> None:
    for entry in dirs:
        wanted = entry.package.dir_mode
        log.info("    %s", entry.path)
        if not entry.exists:
            log.info("        state    absent  =>  created")
            log.info("        mode     -  =>  %s", wanted)
            log.info("        owner    -  =>  %s", entry.package.owner)
        else:
            marker = "   CHANGES" if entry.mode != wanted else ""
            log.info("        state    exists")
            log.info("        mode     %s  =>  %s%s", entry.mode, wanted, marker)
            log.info("        owner    %s (left alone)", entry.owner)
        for verdict in entry.decisions or ["nothing to change"]:
            log.info("        DECISION %s", verdict)


def check_sudoers(entry: Entry) -> str | None:
    """Syntax-check one candidate sudoers snippet. None means it is fine."""
    source = REPO / entry.source
    cmd = ["visudo", "-c", "-f", str(source)]
    log.info("spawn: %s", " ".join(cmd))
    done = subprocess.run(
        cmd, check=False, capture_output=True, text=True, timeout=SPAWN_TIMEOUT
    )
    if done.returncode == 0:
        log.info("    sudoers syntax ok: %s", entry.source)
        return None
    # visudo reports the offending line on stderr, the summary on stdout
    detail = (done.stderr.strip() or done.stdout.strip()).replace("\n", "; ")
    return f"{entry.source}: {detail}"


def gate_sudoers(packages: list[Package]) -> bool:
    """Re-check sudoers as installed. This is the owner and mode gate.

    Takes every package that contributed a gated file rather than one of them:
    a pathless `visudo -c` reads the system sudoers, so it needs root whenever
    any contributor writes as root. Picking a single package would let a
    `sudo: false` one run the check unprivileged and report a false failure on
    a perfectly good sudoers tree.
    """
    cmd = ["visudo", "-c"]
    if any(package.sudo for package in packages):
        cmd = ["sudo", *cmd]
    log.info("spawn: %s", " ".join(cmd))
    done = subprocess.run(
        cmd, check=False, capture_output=True, text=True, timeout=SPAWN_TIMEOUT
    )
    for line in done.stdout.splitlines():
        log.info("    %s", line)
    return done.returncode == 0


GATES = {"sudoers": gate_sudoers}


def preflight(pending: list[Change]) -> list[str]:
    """Validate every candidate before anything is written. Empty means go.

    Runs as one pass over the whole set, across every package, so a file that
    would be rejected stops the run with the system untouched rather than after
    N paths have already been replaced.
    """
    problems = []
    for change in pending:
        entry = change.entry
        rule = entry.rule
        if rule is None or rule.validate is None:
            continue
        if rule.validate == "sudoers" and (problem := check_sudoers(entry)):
            problems.append(problem)
    return problems


def post_commands(packages: list[Package]) -> dict[tuple[str, ...], list[str]]:
    """Post-install commands to run, deduped, in declaration order.

    Two packages that both drop systemd units both ask for a daemon-reload.
    Running it once is the same outcome and a far shorter log, so identical
    resolved argv collapse, remembering every package that asked for it.
    """
    wanted: dict[tuple[str, ...], list[str]] = {}
    for package in packages:
        for cmd in package.post:
            wanted.setdefault(tuple(package.privileged(list(cmd))), []).append(
                package.name
            )
    return wanted


def run_post(packages: list[Package], *, dry_run: bool) -> None:
    """Run every selected package's post commands, whatever this run installed.

    Deliberately not conditional on having installed something. These are
    reloads -- `daemon-reload`, `udevadm control --reload`, `sysctl --system`
    -- and they are what makes an installed unit or rule take effect. A run
    that installs files and then dies before reloading would otherwise leave
    the next run reporting "0 need work" and never reloading at all, so a
    fresh unit stays unknown to systemd until reboot with nothing saying so.
    They are idempotent and cheap; skipping them is the only expensive option.
    """
    wanted = post_commands(packages)
    if not wanted:
        return
    log.info("%s", "-" * 60)
    log.info("post-install")
    for cmd, asked in wanted.items():
        log.info("    asked for by %s", ", ".join(asked))
        if dry_run:
            log.info("    would run: %s", " ".join(cmd))
        else:
            run(list(cmd))


def install_file(entry: Entry) -> str:
    """Put one repo file at its destination with its declared mode and owner."""
    package = entry.package
    user, group = package.owner.split(":")
    run(
        package.privileged(
            ["install", "-d", "-m", package.dir_mode, str(entry.dest.parent)]
        )
    )
    place = package.privileged(
        [
            "install",
            "-m",
            entry.mode,
            "-o",
            user,
            "-g",
            group,
            str(REPO / entry.source),
        ]
    )

    rule = entry.rule
    write = rule.write if rule else "install"
    if write == "install":
        run([*place, str(entry.dest)])
        return write

    # Same directory, so the rename stays on one filesystem.
    staged = entry.dest.with_name(f"{entry.dest.name}.installing")
    run([*place, str(staged)])
    run(package.privileged(["mv", "-T", str(staged), str(entry.dest)]))
    return write


def run(cmd: list[str], *, dry_run: bool = False) -> None:
    log.info("spawn: %s", " ".join(cmd))
    if dry_run:
        return
    subprocess.run(
        cmd, check=True, stdout=sys.stderr, stderr=sys.stderr, timeout=SPAWN_TIMEOUT
    )


def install(args: argparse.Namespace) -> int:
    available = installable()
    names = args.packages or available
    unknown = [name for name in names if name not in available]
    if unknown:
        log.error("no %s in: %s", CONFIG, ", ".join(unknown))
        log.error("packages this script installs: %s", ", ".join(available))
        return 1

    packages = [load(name) for name in names]
    for package in packages:
        log.info(
            "%s -> %s (owner %s%s)",
            package.name,
            package.target,
            package.owner,
            ", sudo" if package.sudo else "",
        )

    changes = [inspect(e) for p in packages for e in manifest(p)]
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
        run_post(packages, dry_run=args.dry_run)
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
        run_post(packages, dry_run=True)
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
        # Keyed by source, package segment included: two packages can otherwise
        # hold the same relative path and clobber each other's backup.
        saved = backup / entry.source
        saved.parent.mkdir(parents=True, exist_ok=True)
        saved.write_bytes(read_strict(entry.dest))
        saved_entries.append(record(change, entry.source))
        log.info("backed up %s to %s", entry.dest, saved)

    notes = backup / MANIFEST
    notes.parent.mkdir(parents=True, exist_ok=True)
    notes.write_text(
        json.dumps(
            {
                "created": stamp.isoformat(timespec="seconds"),
                "repo": str(REPO),
                "backup": str(backup),
                "packages": [p.name for p in packages],
                "entries": saved_entries,
            },
            indent=2,
        )
        + "\n"
    )
    log.info("%s", "-" * 60)
    log.info("every replaced file is backed up; wrote %s", notes)
    log.info("installing")

    gated: dict[str, list[Package]] = {}
    for change in pending:
        entry = change.entry
        write = install_file(entry)
        rule = entry.rule
        if rule is not None and rule.validate is not None:
            gated.setdefault(rule.validate, [])
            if entry.package not in gated[rule.validate]:
                gated[rule.validate].append(entry.package)
        log.info(
            "installed %s%s", entry.dest, "" if write == "install" else f" ({write})"
        )

    for strategy, contributors in gated.items():
        log.info("%s", "-" * 60)
        log.info(
            "verifying %s as installed (%s)",
            strategy,
            ", ".join(p.name for p in contributors),
        )
        if not GATES[strategy](contributors):
            log.error("%s is NOT valid; restore from %s now", strategy, backup)
            return 1

    run_post(packages, dry_run=False)

    log.info("%s", "-" * 60)
    log.info("replaced files backed up under %s", backup)
    log.info("what each path was before is in %s", notes)
    log.info("restore one: read its entry there, then")
    log.info("  cp %s/<saved> <dest>", backup)
    log.info("  chmod <previous.mode> <dest> && chown <previous.owner> <dest>")
    log.info("  (prefix both with sudo for a package that declares it)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "packages",
        nargs="*",
        help=f"Packages to install. Default: every one with a {CONFIG}.",
    )
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
