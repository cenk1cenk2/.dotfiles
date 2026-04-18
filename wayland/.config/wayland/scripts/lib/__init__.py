"""Shared building blocks for the Wayland scripts in this folder.

Import via `from lib import …` — this __init__ re-exports everything the
scripts need so callers never reach for a submodule directly.
"""

from .enrich import (
    DEFAULT_MODEL,
    EnrichAdapterClaude,
    EnrichAdapterCodex,
    EnrichAdapter,
    EnrichProvider,
    EnrichAdapterHttp,
)
from .input import (
    InputAdapterClipboard,
    InputAdapter,
    InputMode,
    InputAdapterStdin,
)
from .notify import notify
from .output import (
    OutputAdapterClipboard,
    OutputAdapter,
    OutputMode,
    OutputAdapterStdout,
    OutputAdapterType,
)
from .prompts import load_prompt
from .waybar import signal_waybar

__all__ = [
    "DEFAULT_MODEL",
    "EnrichAdapter",
    "EnrichAdapterClaude",
    "EnrichAdapterCodex",
    "EnrichAdapterHttp",
    "EnrichProvider",
    "InputAdapter",
    "InputAdapterClipboard",
    "InputAdapterStdin",
    "InputMode",
    "OutputAdapter",
    "OutputAdapterClipboard",
    "OutputAdapterStdout",
    "OutputAdapterType",
    "OutputMode",
    "load_prompt",
    "notify",
    "signal_waybar",
]
