"""Shared CLI + logging scaffolding for Hyprland scripts."""

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
        for handler in list(root.handlers):
            root.removeHandler(handler)
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
        for handler in root.handlers:
            handler.setLevel(level)

    return logging.getLogger(name) if name else root
