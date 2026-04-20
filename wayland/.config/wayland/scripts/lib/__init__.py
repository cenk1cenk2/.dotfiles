"""Shared building blocks for the Wayland scripts in this folder.

Re-exports follow the `X as X` form so Ruff treats them as explicit
public re-exports (silences F401) and LSP rename still works — those
are real imported names, not string literals in `__all__`.

`lib.overlay` is NOT re-exported here on purpose: its GTK / gtk4-
layer-shell imports are heavy and useless to headless callers
(waybar status polls, MCP subprocess). Pilot (the only overlay
consumer) imports directly via `from lib.overlay import …`."""

from .acp_adapter import ModelChoice as ModelChoice
from .cli import (
    RunResult as RunResult,
    create_logger as create_logger,
    run as run,
)
from .converse import (
    DEFAULT_CONVERSE_ADAPTER as DEFAULT_CONVERSE_ADAPTER,
    ConversationAdapter as ConversationAdapter,
    ConversationAdapterClaude as ConversationAdapterClaude,
    ConversationAdapterOpenCode as ConversationAdapterOpenCode,
    ConversationProvider as ConversationProvider,
    PlanChunk as PlanChunk,
    PromptAttachment as PromptAttachment,
    SessionInfoChunk as SessionInfoChunk,
    ThinkingChunk as ThinkingChunk,
    ToolCall as ToolCall,
    UserMessageChunk as UserMessageChunk,
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
from .layer_shell import ensure_layer_shell_preload as ensure_layer_shell_preload
from .markup import (
    CodeBlock as CodeBlock,
    MarkdownBlock as MarkdownBlock,
    MarkdownMarkup as MarkdownMarkup,
    TextBlock as TextBlock,
    highlight_code as highlight_code,
)
from .mcp_servers import (
    DEFAULT_SERVER_NAMES as DEFAULT_SERVER_NAMES,
    DEFAULT_SERVERS as DEFAULT_SERVERS,
    get_permission_seeds as get_permission_seeds,
    get_server as get_server,
)
from .notify import notify as notify
from .output import (
    OutputAdapter as OutputAdapter,
    OutputAdapterClipboard as OutputAdapterClipboard,
    OutputAdapterStdout as OutputAdapterStdout,
    OutputAdapterType as OutputAdapterType,
    OutputMode as OutputMode,
)
from .permissions import (
    PermissionKind as PermissionKind,
    PermissionState as PermissionState,
    normalise_tool_name as normalise_tool_name,
)
from .prompts import load_prompt as load_prompt
from .prompts import load_relative_file as load_relative_file
from .tools import ToolFormatters as ToolFormatters
from .waybar import signal_waybar as signal_waybar
