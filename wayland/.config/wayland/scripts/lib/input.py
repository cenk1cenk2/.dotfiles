"""Input sources that provide the text to process."""

import logging
import subprocess
import sys
from enum import StrEnum
from typing import Protocol

log = logging.getLogger(__name__)

class InputMode(StrEnum):
    CLIPBOARD = "clipboard"
    STDIN = "stdin"

class InputAdapter(Protocol):
    mode: InputMode

    def read(self) -> str | None:
        """Return the text to process, or None on failure."""
        ...

class InputAdapterClipboard:
    mode = InputMode.CLIPBOARD

    def read(self) -> str | None:
        cmd = ["wl-paste", "--no-newline"]
        log.debug("spawn: %s", " ".join(cmd))
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            log.error("wl-paste failed: %s", e)
            return None
        log.debug("wl-paste stderr: %s", result.stderr.strip())
        return result.stdout

    @staticmethod
    def list_mime_types() -> list[str]:
        """MIME types advertised for the current clipboard selection."""
        cmd = ["wl-paste", "--list-types"]
        log.debug("spawn: %s", " ".join(cmd))
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        except subprocess.CalledProcessError, FileNotFoundError:
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    @staticmethod
    def read_binary(mime: str) -> bytes | None:
        """Clipboard payload for `mime` as raw bytes."""
        cmd = ["wl-paste", "--no-newline", "--type", mime]
        log.debug("spawn: %s", " ".join(cmd))
        try:
            result = subprocess.run(cmd, capture_output=True, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            log.debug("wl-paste %s failed: %s", mime, e)
            return None
        return result.stdout or None

class InputAdapterStdin:
    mode = InputMode.STDIN

    def read(self) -> str | None:
        try:
            return sys.stdin.read()
        except Exception as e:
            log.error("stdin read failed: %s", e)
            return None
