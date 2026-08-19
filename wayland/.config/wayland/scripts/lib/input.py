"""Input sources that provide the payload to process."""

import logging
import subprocess
import sys
from enum import StrEnum
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)

class InputMode(StrEnum):
    CLIPBOARD = "clipboard"
    STDIN = "stdin"
    FILE = "file"

class InputAdapter(Protocol):
    mode: InputMode
    # Names the payload for backends that upload it; only the file
    # adapter knows a real one.
    name: str

    def read(self) -> str | None:
        """Return the text to process, or None on failure."""
        ...

    def read_bytes(self) -> bytes | None:
        """Return the raw payload, or None on failure."""
        ...

class InputAdapterClipboard:
    mode = InputMode.CLIPBOARD
    name = "clipboard"

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

    def read_bytes(self) -> bytes | None:
        types = self.list_mime_types()
        mime = next((t for t in types if t.startswith("audio/")), None)
        if mime is None:
            log.error("clipboard holds no audio: %s", types)
            return None
        self.name = f"clipboard.{mime.removeprefix('audio/')}"

        return self.read_binary(mime)

class InputAdapterStdin:
    mode = InputMode.STDIN
    name = "stdin"

    def read(self) -> str | None:
        try:
            return sys.stdin.read()
        except Exception as e:
            log.error("stdin read failed: %s", e)
            return None

    def read_bytes(self) -> bytes | None:
        try:
            return sys.stdin.buffer.read()
        except Exception as e:
            log.error("stdin read failed: %s", e)
            return None

class InputAdapterFile:
    mode = InputMode.FILE

    def __init__(self, path: Path):
        self.path = path
        self.name = path.name

    def read(self) -> str | None:
        try:
            return self.path.read_text(encoding="utf-8")
        except OSError as e:
            log.error("read failed: %s", e)
            return None

    def read_bytes(self) -> bytes | None:
        try:
            return self.path.read_bytes()
        except OSError as e:
            log.error("read failed: %s", e)
            return None

def build_input(mode: InputMode, **kwargs) -> InputAdapter:
    """Adapter for `mode`, handed whatever that adapter takes.

    Unset knobs drop out, so a caller can pass every flag it has and let
    the mode decide. One that is set but wrong reaches the constructor
    and raises there rather than being silently ignored."""
    kwargs = {k: v for k, v in kwargs.items() if v is not None}
    try:
        match mode:
            case InputMode.CLIPBOARD:
                return InputAdapterClipboard(**kwargs)
            case InputMode.STDIN:
                return InputAdapterStdin(**kwargs)
            case InputMode.FILE:
                if kwargs.get("path") is None:
                    raise ValueError("file input requires a path")
                return InputAdapterFile(**kwargs)
            case _:
                raise ValueError(f"unsupported input mode: {mode!r}")
    except TypeError as e:
        raise ValueError(f"{mode.value} input takes no {', '.join(kwargs)}") from e
