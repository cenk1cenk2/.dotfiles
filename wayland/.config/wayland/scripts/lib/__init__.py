"""Shared building blocks for the Wayland scripts in this folder.

Import via `from lib import …` — this __init__ re-exports everything the
scripts need so callers never reach for a submodule directly.
"""

from .enrich import (
    DEFAULT_MODEL,
    ClaudeEnrichAdapter,
    CodexEnrichAdapter,
    EnrichAdapter,
    EnrichProvider,
    HttpEnrichAdapter,
)
from .input import (
    ClipboardInputAdapter,
    InputAdapter,
    InputMode,
    StdinInputAdapter,
)
from .notify import notify
from .output import (
    ClipboardOutputAdapter,
    OutputAdapter,
    OutputMode,
    StdoutOutputAdapter,
    TypeOutputAdapter,
)
from .prompts import load_prompt
from .waybar import signal_waybar

__all__ = [
    "DEFAULT_MODEL",
    "ClaudeEnrichAdapter",
    "ClipboardInputAdapter",
    "ClipboardOutputAdapter",
    "CodexEnrichAdapter",
    "EnrichAdapter",
    "EnrichProvider",
    "HttpEnrichAdapter",
    "InputAdapter",
    "InputMode",
    "OutputAdapter",
    "OutputMode",
    "StdinInputAdapter",
    "StdoutOutputAdapter",
    "TypeOutputAdapter",
    "load_prompt",
    "notify",
    "signal_waybar",
]
