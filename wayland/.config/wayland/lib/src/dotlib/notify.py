"""Desktop notification helper."""

import logging
import subprocess
import sys

from .desktop import is_headless

log = logging.getLogger(__name__)


def notify(
    title: str,
    message: str,
    icon: str,
    timeout: int | None = None,
) -> None:
    """Send a desktop notification via notify-send. Failures are swallowed."""
    if is_headless():
        log.debug("headless: dropping notification %r", message)
        return

    cmd = ["notify-send", title, message, "-i", icon]
    if timeout:
        cmd.extend(["-t", str(timeout)])
    log.debug("spawn: %s", " ".join(cmd))
    subprocess.run(cmd, check=False, stdout=sys.stderr, stderr=sys.stderr)
