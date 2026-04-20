"""Output sinks that write final transcription text for the user."""

import logging
import subprocess
import sys
from enum import StrEnum
from typing import Protocol

log = logging.getLogger(__name__)


class OutputMode(StrEnum):
    CLIPBOARD = "clipboard"
    TYPE = "type"
    STDOUT = "stdout"


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
        cmd = ["ydotool", "type", "--key-delay", "10", "--key-hold", "10", "--file", "-"]
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
