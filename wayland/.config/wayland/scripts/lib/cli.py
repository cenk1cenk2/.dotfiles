"""Shared CLI + logging scaffolding for the wayland scripts.

Every entry script wires its click root through `create_logger` so
`--verbose` bumps the root to DEBUG and everything else stays INFO.
Rich handler, stderr-bound — stdout is reserved for pipe-friendly
command output (waybar JSON, stdout sinks)."""

from __future__ import annotations

import logging
import sys
from typing import Optional

from rich.console import Console
from rich.logging import RichHandler

_console: Optional[Console] = None


def create_logger(verbose: bool, *, name: Optional[str] = None) -> logging.Logger:
    """Install a rich handler on the root logger, bound to stderr."""
    global _console
    root = logging.getLogger()
    level = logging.DEBUG if verbose else logging.INFO
    root.setLevel(level)

    if not any(isinstance(h, RichHandler) for h in root.handlers):
        if _console is None:
            _console = Console(file=sys.stderr, stderr=True, force_terminal=None)
        for h in list(root.handlers):
            root.removeHandler(h)
        handler = RichHandler(
            console=_console,
            show_path=False,
            show_time=True,
            rich_tracebacks=True,
            markup=False,
            log_time_format="[%H:%M:%S]",
        )
        handler.setLevel(level)
        root.addHandler(handler)
    else:
        for h in root.handlers:
            h.setLevel(level)

    return logging.getLogger(name) if name else root
