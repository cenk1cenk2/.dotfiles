"""Desktop notification helper."""

import subprocess
from typing import Optional

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
    subprocess.run(cmd, check=False)
