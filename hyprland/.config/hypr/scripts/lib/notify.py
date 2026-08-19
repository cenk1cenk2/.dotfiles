import subprocess

def notify(
    title: str,
    message: str,
    icon: str | None = None,
    urgency: str | None = None,
    timeout: int | None = None,
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
