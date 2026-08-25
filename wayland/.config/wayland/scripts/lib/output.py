"""Output sinks that write final transcription text for the user."""

import logging
import subprocess
import sys
from enum import StrEnum
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)


class OutputMode(StrEnum):
    CLIPBOARD = "clipboard"
    TYPE = "type"
    STDOUT = "stdout"
    FILE = "file"


class OutputAdapter(Protocol):
    mode: OutputMode

    def write(self, text: str) -> None:
        """Emit the text. Blocking; raises on failure."""
        ...


class OutputAdapterClipboard:
    mode = OutputMode.CLIPBOARD

    def write(self, text: str) -> None:
        cmd = ["wl-copy"]
        log.debug("spawn: %s (%d chars)", " ".join(cmd), len(text))
        subprocess.run(
            cmd,
            input=text,
            text=True,
            check=False,
            stdout=sys.stderr,
            stderr=sys.stderr,
        )


class OutputAdapterType:
    mode = OutputMode.TYPE

    def write(self, text: str) -> None:
        cmd = [
            "ydotool",
            "type",
            "--key-delay",
            "10",
            "--key-hold",
            "10",
            "--file",
            "-",
        ]
        log.debug("spawn: %s (%d chars)", " ".join(cmd), len(text))
        subprocess.run(
            cmd,
            input=text,
            text=True,
            check=False,
            stdout=sys.stderr,
            stderr=sys.stderr,
        )


class OutputAdapterStdout:
    mode = OutputMode.STDOUT

    def write(self, text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()


class OutputAdapterFile:
    mode = OutputMode.FILE

    def __init__(self, path: Path):
        self.path = path

    def write(self, text: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(text, encoding="utf-8")
        log.debug("wrote %d chars to %s", len(text), self.path)


def build_output(mode: OutputMode, **kwargs) -> OutputAdapter:
    """Adapter for `mode`, handed whatever that adapter takes.

    Unset knobs drop out, so a caller can pass every flag it has and let
    the mode decide. One that is set but wrong reaches the constructor
    and raises there rather than being silently ignored."""
    kwargs = {k: v for k, v in kwargs.items() if v is not None}
    try:
        match mode:
            case OutputMode.CLIPBOARD:
                return OutputAdapterClipboard(**kwargs)
            case OutputMode.TYPE:
                return OutputAdapterType(**kwargs)
            case OutputMode.STDOUT:
                return OutputAdapterStdout(**kwargs)
            case OutputMode.FILE:
                if kwargs.get("path") is None:
                    raise ValueError("file output requires a path")
                return OutputAdapterFile(**kwargs)
            case _:
                raise ValueError(f"unsupported output mode: {mode!r}")
    except TypeError as e:
        raise ValueError(f"{mode.value} output takes no {', '.join(kwargs)}") from e
