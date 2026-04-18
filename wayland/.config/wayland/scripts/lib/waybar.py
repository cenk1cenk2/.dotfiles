"""Waybar signalling helper."""

import subprocess
import sys

def signal_waybar(module: str) -> None:
    """Poke waybar to re-render the named custom module.

    waybar-signal.sh prints an informational `Sending waybar signal: …`
    line on success and a `No signal mapped for …` line on failure. We
    route both to our stderr so nothing leaks into a piped stdout — this
    helper runs inside scripts whose stdout is semantically meaningful
    (e.g. `speech.py --output stdout | ask.py --input stdin`, or the
    waybar custom-module JSON emitted by `ask.py status`)."""
    subprocess.run(
        ["waybar-signal.sh", module],
        check=False,
        stdout=sys.stderr,
        stderr=sys.stderr,
    )
