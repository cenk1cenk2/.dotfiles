"""Waybar signalling helper."""

import logging
import subprocess
import sys

log = logging.getLogger(__name__)


def signal_waybar(module: str) -> None:
    """Poke waybar to re-render the named custom module. Output is
    routed to stderr so nothing leaks into pipeable stdout."""
    cmd = ["waybar-signal.sh", module]
    log.debug("spawn: %s", " ".join(cmd))
    subprocess.run(cmd, check=False, stdout=sys.stderr, stderr=sys.stderr)
