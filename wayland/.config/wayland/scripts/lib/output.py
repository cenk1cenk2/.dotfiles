"""Output sinks that write final transcription text for the user."""

import subprocess
import sys
from typing import Protocol
from enum import StrEnum

class OutputMode(StrEnum):
    CLIPBOARD = "clipboard"
    TYPE = "type"
    STDOUT = "stdout"

class OutputAdapter(Protocol):
    """Sink that writes final text somewhere visible to the user."""

    mode: OutputMode

    def write(self, text: str) -> None:
        """Emit the text. Blocking; raises on failure."""
        ...

class ClipboardOutputAdapter:
    """Copies text to the Wayland clipboard via `wl-copy`."""

    mode = OutputMode.CLIPBOARD

    def write(self, text: str) -> None:
        subprocess.run(["wl-copy"], input=text, text=True, check=False)

class TypeOutputAdapter:
    """Types text into the focused window via `ydotool`."""

    mode = OutputMode.TYPE

    def write(self, text: str) -> None:
        subprocess.run(
            [
                "ydotool",
                "type",
                "--key-delay",
                "10",
                "--key-hold",
                "10",
                "--file",
                "-",
            ],
            input=text,
            text=True,
            check=False,
        )

class StdoutOutputAdapter:
    """Writes text to stdout."""

    mode = OutputMode.STDOUT

    def write(self, text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()
