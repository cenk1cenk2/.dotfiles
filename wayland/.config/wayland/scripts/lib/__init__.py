"""Shared building blocks for the Wayland scripts in this folder.

Import via `from lib import …` — this __init__ re-exports everything the
scripts need so callers never reach for a submodule directly.
"""

from .converse import (
    DEFAULT_CONVERSE_ADAPTER,
    ConversationAdapter,
    ConversationAdapterClaude,
    ConversationAdapterCodex,
    ConversationAdapterHttp,
    ConversationAdapterOpenCode,
    ConversationProvider,
    ThinkingChunk,
    ToolCall,
)
from .enrich import (
    DEFAULT_ENRICH_ADAPTER,
    EnrichAdapterClaude,
    EnrichAdapterCodex,
    EnrichAdapter,
    EnrichAdapterOpenCode,
    EnrichProvider,
    EnrichAdapterHttp,
)
from .input import (
    InputAdapterClipboard,
    InputAdapter,
    InputMode,
    InputAdapterStdin,
)
from .mcp import (
    McpConfig,
    McpServer,
    question_route,
    socket_approval,
    socket_question,
)
from .notify import notify
from .output import (
    OutputAdapterClipboard,
    OutputAdapter,
    OutputMode,
    OutputAdapterStdout,
    OutputAdapterType,
)
from .prompts import load_prompt, load_relative_file
from .waybar import signal_waybar

__all__ = [
    "ConversationAdapter",
    "ConversationAdapterClaude",
    "ConversationAdapterCodex",
    "ConversationAdapterHttp",
    "ConversationAdapterOpenCode",
    "ConversationProvider",
    "DEFAULT_CONVERSE_ADAPTER",
    "DEFAULT_ENRICH_ADAPTER",
    "EnrichAdapter",
    "EnrichAdapterClaude",
    "EnrichAdapterCodex",
    "EnrichAdapterHttp",
    "EnrichAdapterOpenCode",
    "EnrichProvider",
    "InputAdapter",
    "InputAdapterClipboard",
    "InputAdapterStdin",
    "InputMode",
    "McpConfig",
    "McpServer",
    "OutputAdapter",
    "OutputAdapterClipboard",
    "OutputAdapterStdout",
    "OutputAdapterType",
    "OutputMode",
    "ThinkingChunk",
    "ToolCall",
    "load_prompt",
    "load_relative_file",
    "notify",
    "question_route",
    "signal_waybar",
    "socket_approval",
    "socket_question",
]
