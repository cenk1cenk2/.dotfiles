"""Input sources that provide the text to process."""

import logging
import subprocess
import sys
from enum import StrEnum
from typing import Optional, Protocol

log = logging.getLogger(__name__)

class InputMode(StrEnum):
    CLIPBOARD = "clipboard"
    STDIN = "stdin"

class InputAdapter(Protocol):
    """Source that provides text for processing."""

    mode: InputMode

    def read(self) -> Optional[str]:
        """Return the text to process, or None on failure."""
        ...

class InputAdapterClipboard:
    """Reads text from the Wayland clipboard via `wl-paste`."""

    mode = InputMode.CLIPBOARD

    def read(self) -> Optional[str]:
        try:
            result = subprocess.run(
                ["wl-paste", "--no-newline"],
                capture_output=True,
                text=True,
                check=True,
            )

            return result.stdout
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            log.error("failed to read clipboard: %s", e)
            return None

class InputAdapterStdin:
    """Reads text from the process's standard input until EOF."""

    mode = InputMode.STDIN

    def read(self) -> Optional[str]:
        try:
            return sys.stdin.read()
        except Exception as e:
            log.error("failed to read stdin: %s", e)
            return None
