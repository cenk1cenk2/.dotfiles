"""Shared building blocks for the Wayland scripts in this folder.

Import via `from lib import …` — this __init__ re-exports everything the
scripts need so callers never reach for a submodule directly.

`lib.overlay` is the exception — its GTK / gtk4-layer-shell imports are
heavy and unwanted in headless subprocesses (e.g. `pilot.py mcp-server`).
We expose its symbols via a lazy `__getattr__` at the bottom of this
module so `from lib import LayerOverlayWindow` works for GUI callers
without making `from lib.mcp import McpServer` drag gi into the MCP
stdio subprocess.
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
from .default_servers import (
    DEFAULT_SERVER_NAMES,
    DEFAULT_SERVERS,
    get_server,
)
from .mcp import (
    AutoCheckPassthrough,
    McpCapability,
    McpConfig,
    McpServer,
    question_route,
    socket_approval,
    socket_auto_check,
    socket_question,
)
from .notify import notify
from .pango_highlight import highlight_code
from .output import (
    OutputAdapterClipboard,
    OutputAdapter,
    OutputMode,
    OutputAdapterStdout,
    OutputAdapterType,
)
from .prompts import load_prompt, load_relative_file
from .tool_format import format_tool_args
from .waybar import signal_waybar

_OVERLAY_EXPORTS = frozenset({
    "ButtonVariant",
    "CommandPalette",
    "CommandPaletteEntry",
    "Header",
    "LayerOverlayWindow",
    "PillVariant",
    "ensure_layer_shell_preload",
    "focused_gdk_monitor",
    "focused_monitor_name",
    "load_css_from_path",
    "load_overlay_css",
    "make_button",
    "make_card",
    "make_collapsible",
    "make_pill",
})


def __getattr__(name: str):
    """Lazy re-export for `lib.overlay` symbols. Keeps `from lib import
    LayerOverlayWindow` working while `from lib.mcp import McpServer`
    continues to load without pulling in gi / Gtk — the mcp-server
    subprocess path depends on that."""
    if name in _OVERLAY_EXPORTS:
        from . import overlay as _overlay_module

        return getattr(_overlay_module, name)
    raise AttributeError(f"module 'lib' has no attribute {name!r}")


__all__ = [
    "AutoCheckPassthrough",
    "ButtonVariant",
    "CommandPalette",
    "CommandPaletteEntry",
    "ConversationAdapter",
    "ConversationAdapterClaude",
    "ConversationAdapterCodex",
    "ConversationAdapterHttp",
    "ConversationAdapterOpenCode",
    "ConversationProvider",
    "DEFAULT_CONVERSE_ADAPTER",
    "DEFAULT_ENRICH_ADAPTER",
    "DEFAULT_SERVER_NAMES",
    "DEFAULT_SERVERS",
    "EnrichAdapter",
    "EnrichAdapterClaude",
    "EnrichAdapterCodex",
    "EnrichAdapterHttp",
    "EnrichAdapterOpenCode",
    "EnrichProvider",
    "Header",
    "InputAdapter",
    "InputAdapterClipboard",
    "InputAdapterStdin",
    "InputMode",
    "LayerOverlayWindow",
    "McpCapability",
    "McpConfig",
    "McpServer",
    "OutputAdapter",
    "OutputAdapterClipboard",
    "OutputAdapterStdout",
    "OutputAdapterType",
    "OutputMode",
    "PillVariant",
    "ThinkingChunk",
    "ToolCall",
    "ensure_layer_shell_preload",
    "focused_gdk_monitor",
    "focused_monitor_name",
    "format_tool_args",
    "get_server",
    "highlight_code",
    "load_css_from_path",
    "load_overlay_css",
    "load_prompt",
    "load_relative_file",
    "make_button",
    "make_card",
    "make_collapsible",
    "make_pill",
    "notify",
    "question_route",
    "signal_waybar",
    "socket_approval",
    "socket_auto_check",
    "socket_question",
]
