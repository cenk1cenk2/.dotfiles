"""Waybar signalling helper."""

import logging
import subprocess
import sys

from .desktop import is_headless

log = logging.getLogger(__name__)

def signal_waybar(module: str) -> None:
    """Poke waybar to re-render the named custom module. Output is
    routed to stderr so nothing leaks into pipeable stdout."""
    if is_headless():
        log.debug("headless: skipping signal for %s", module)
        return

    cmd = ["waybar-signal.sh", module]
    log.debug("spawn: %s", " ".join(cmd))
    subprocess.run(cmd, check=False, stdout=sys.stderr, stderr=sys.stderr)
