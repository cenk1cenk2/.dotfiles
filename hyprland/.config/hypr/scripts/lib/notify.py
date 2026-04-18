import subprocess
from typing import Optional

def notify(
    title: str,
    message: str,
    icon: Optional[str] = None,
    urgency: Optional[str] = None,
    timeout: Optional[int] = None,
) -> None:
    """Send a notify-send notification. Failures are swallowed."""
    cmd = ["notify-send", title, message]
    if icon:
        cmd.extend(["-i", icon])
    if urgency:
        cmd.extend(["-u", urgency])
    if timeout:
        cmd.extend(["-t", str(timeout)])
    subprocess.run(cmd, check=False)
