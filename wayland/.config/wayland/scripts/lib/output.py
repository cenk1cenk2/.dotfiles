"""Output sinks that write final transcription text for the user."""

import logging
import subprocess
import sys
import time
from collections.abc import Iterable
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

log = logging.getLogger(__name__)


class OutputMode(StrEnum):
    CLIPBOARD = "clipboard"
    PASTE = "paste"
    STDOUT = "stdout"
    FILE = "file"


class OutputAdapter(Protocol):
    mode: OutputMode

    def write(self, text: str) -> None:
        """Emit the text. Blocking; raises on failure."""
        ...


@runtime_checkable
class OutputStreaming(Protocol):
    """Sink that can take the text as it arrives rather than in one piece.

    Only a sink whose target is append-only qualifies: the clipboard and a file
    each hold one value, so a partial rewrite in them is not an early result but
    a wrong one. Callers ask by `isinstance` and fall back to `write`."""

    def write_stream(self, chunks: Iterable[str]) -> None: ...


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


class OutputAdapterPaste:
    """Pastes into the focused window, through the clipboard.

    Typed keys turn every newline into an Enter, which submits a chat or an
    agent prompt once per line, and go through a keyboard layout that may
    not hold every character. A paste lands the text whole, as a bracketed
    paste in a terminal. Shift+Insert is the paste key GTK, Qt, Chromium,
    Firefox and Electron share, and kitty maps it to the clipboard in
    `kitty.conf`.

    The text stays on the clipboard, so a paste that landed in the wrong
    window can be repeated by hand. Not streaming: a paste cannot append.

    `submit` presses Enter after it, for a prompt that should go out as it
    lands. The paste key only asks the app to fetch the clipboard, which it
    does asynchronously, so an Enter sent at once can submit before the text
    is in; SUBMIT_DELAY waits that out."""

    mode = OutputMode.PASTE

    KEYS = ("wtype", "-M", "shift", "-k", "Insert", "-m", "shift")
    SUBMIT = ("wtype", "-k", "Return")
    SUBMIT_DELAY = 0.3

    def __init__(self, submit: bool = False):
        self.submit = submit

    def write(self, text: str) -> None:
        OutputAdapterClipboard().write(text)
        keys = [list(self.KEYS)] + ([list(self.SUBMIT)] if self.submit else [])
        for i, cmd in enumerate(keys):
            if i:
                time.sleep(self.SUBMIT_DELAY)
            log.debug("spawn: %s", " ".join(cmd))
            subprocess.run(cmd, check=False, stdout=sys.stderr, stderr=sys.stderr)


class OutputAdapterStdout:
    mode = OutputMode.STDOUT

    def write(self, text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    def write_stream(self, chunks: Iterable[str]) -> None:
        """Write each chunk as it arrives, flushing so a reader sees it now.

        The pipe closes only after the last chunk, so a consumer reading to EOF
        sees no difference and one reading by line starts before the end."""
        written = 0
        for chunk in chunks:
            sys.stdout.write(chunk)
            sys.stdout.flush()
            written += len(chunk)
        log.info("wrote %d chars as they arrived", written)


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
            case OutputMode.PASTE:
                return OutputAdapterPaste(**kwargs)
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
