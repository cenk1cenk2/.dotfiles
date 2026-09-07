#!/usr/bin/env python3
"""Set the Claude Code config keys `settings.json` cannot carry.

Everything else about a profile is stowed from `claude/` and needs nothing
here. `messageIdleNotifThresholdMs` is the exception: it has no entry in the
settings schema, so Claude Code reads it only from the `.claude.json` beside
those files -- app-managed state, outside stow, and rewritten by every live
session. Running this again is how the value survives a new profile, or a
session that wrote the file back without it.

A running session holds its own copy of that file, so a value set underneath
one can be lost. Run this with the profile's sessions closed, or re-run it
after they exit.

Stdlib only, on a plain python3 shebang for the same reason `install.py` is:
it must not need a venv of its own.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

HOME = Path.home()
# The bare `claude` config first, then the per-profile ones hyprpilot points
# CLAUDE_CONFIG_DIR at.
CONFIGS = (
    HOME / ".claude.json",
    HOME / ".claude-kilic" / ".claude.json",
    HOME / ".claude-laravel" / ".claude.json",
)
# 0 raises the idle notification the moment a turn ends rather than 60s later.
# The Notification hook stays quiet while its pane is in focus, which is what
# makes that liveable instead of constant.
KEYS = {"messageIdleNotifThresholdMs": 0}
# What Claude Code writes the file as. Only a config this script creates needs
# it set; an existing one keeps whatever mode it already has.
MODE = 0o600

log = logging.getLogger("claude-config")


def apply(path: Path, *, dry_run: bool) -> bool:
    """Fold KEYS into one config, reporting only the keys it moves."""
    config = json.loads(path.read_text()) if path.exists() else {}
    pending = {key: value for key, value in KEYS.items() if config.get(key) != value}
    if not pending:
        log.info("%s unchanged", path)
        return False

    for key, value in pending.items():
        log.info("%s  %s  %s => %s", path, key, config.get(key, "unset"), value)
    if dry_run:
        return True

    config.update(pending)
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not path.exists()
    path.write_text(json.dumps(config, indent=2) + "\n")
    if fresh:
        path.chmod(MODE)

    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Show what would change and change nothing.",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    moved = sum(apply(path, dry_run=args.dry_run) for path in CONFIGS)
    log.info(
        "%d of %d configs %s",
        moved,
        len(CONFIGS),
        "stale" if args.dry_run else "written",
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
