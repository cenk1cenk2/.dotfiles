"""Desktop notification helper."""

import logging
import subprocess
import sys
from typing import Optional

log = logging.getLogger(__name__)


def notify(
    title: str,
    message: str,
    icon: str,
    timeout: Optional[int] = None,
) -> None:
    """Send a desktop notification via notify-send. Failures are swallowed."""
    cmd = ["notify-send", title, message, "-i", icon]
    if timeout:
        cmd.extend(["-t", str(timeout)])
    log.debug("spawn: %s", " ".join(cmd))
    subprocess.run(cmd, check=False, stdout=sys.stderr, stderr=sys.stderr)
