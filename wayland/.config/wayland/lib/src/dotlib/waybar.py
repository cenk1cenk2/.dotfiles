"""Waybar signalling and continuous-module helpers."""

import logging
import subprocess
import sys
import time
from collections.abc import Callable

from dotlib.desktop import is_headless

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


def watch(render: Callable[[], str], interval: float) -> None:
    """Run as a continuous waybar module: print `render()` once per change.

    One process stays up and reads state in-process, where an `interval`
    module would start the whole script again every tick. An empty `text`
    hides the module, which is what its `exec-if` used to do."""
    last = None
    try:
        while True:
            try:
                line = render()
            except Exception:
                log.exception("status render failed")
            else:
                if line != last:
                    sys.stdout.write(line + "\n")
                    sys.stdout.flush()
                    last = line
            time.sleep(interval)
    except BrokenPipeError:
        return
