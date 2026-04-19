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
    """Reads from the Wayland clipboard via `wl-paste`. Defaults to
    text; `read_binary(mime)` pulls an image / audio / arbitrary
    blob for mime types that don't flatten to UTF-8."""

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

    @staticmethod
    def list_mime_types() -> list[str]:
        """Return every MIME type `wl-paste` advertises for the current
        clipboard selection. Empty list on transport errors."""
        try:
            result = subprocess.run(
                ["wl-paste", "--list-types"],
                capture_output=True,
                text=True,
                check=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    @staticmethod
    def read_binary(mime: str) -> Optional[bytes]:
        """Pull the clipboard payload for `mime` as raw bytes. Returns
        None if the clipboard doesn't expose that type or `wl-paste`
        errors out."""
        try:
            result = subprocess.run(
                ["wl-paste", "--no-newline", "--type", mime],
                capture_output=True,
                check=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            log.debug("wl-paste %s failed: %s", mime, e)
            return None
        return result.stdout or None

class InputAdapterStdin:
    """Reads text from the process's standard input until EOF."""

    mode = InputMode.STDIN

    def read(self) -> Optional[str]:
        try:
            return sys.stdin.read()
        except Exception as e:
            log.error("failed to read stdin: %s", e)
            return None
