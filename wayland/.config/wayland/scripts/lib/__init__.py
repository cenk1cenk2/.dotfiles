"""Shared building blocks for the Wayland scripts in this folder.

Re-exports follow the `X as X` form so Ruff treats them as explicit
public re-exports (silences F401) and LSP rename still works — those
are real imported names, not string literals in `__all__`."""

from .cli import (
    RunResult as RunResult,
    create_logger as create_logger,
    run as run,
)
from .enrich import (
    DEFAULT_ENRICH_ADAPTER as DEFAULT_ENRICH_ADAPTER,
    EnrichAdapter as EnrichAdapter,
    EnrichAdapterClaude as EnrichAdapterClaude,
    EnrichAdapterHttp as EnrichAdapterHttp,
    EnrichAdapterOpenCode as EnrichAdapterOpenCode,
    EnrichProvider as EnrichProvider,
)
from .input import (
    InputAdapter as InputAdapter,
    InputAdapterClipboard as InputAdapterClipboard,
    InputAdapterStdin as InputAdapterStdin,
    InputMode as InputMode,
)
from .notify import notify as notify
from .output import (
    OutputAdapter as OutputAdapter,
    OutputAdapterClipboard as OutputAdapterClipboard,
    OutputAdapterStdout as OutputAdapterStdout,
    OutputAdapterType as OutputAdapterType,
    OutputMode as OutputMode,
)
from .prompts import load_prompt as load_prompt
from .prompts import load_relative_file as load_relative_file
from .waybar import signal_waybar as signal_waybar
