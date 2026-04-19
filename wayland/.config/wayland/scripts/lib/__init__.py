"""Shared building blocks for the Wayland scripts in this folder.

`lib.overlay` is the exception — its GTK / gtk4-layer-shell imports are
heavy and unwanted in headless callers. We expose its symbols via a
lazy `__getattr__` so `from lib import LayerOverlayWindow` works for
GUI callers without making `from lib import …` drag gi in generally."""

from .converse import (
    DEFAULT_CONVERSE_ADAPTER,
    ConversationAdapter,
    ConversationAdapterClaude,
    ConversationAdapterOpenCode,
    ConversationProvider,
    PlanChunk,
    PromptAttachment,
    ThinkingChunk,
    ToolCall,
    image_attachment,
)
from .mcp_servers import (
    DEFAULT_SERVER_NAMES,
    DEFAULT_SERVERS,
    get_permission_seeds,
    get_server,
)
from .enrich import (
    DEFAULT_ENRICH_ADAPTER,
    EnrichAdapter,
    EnrichAdapterClaude,
    EnrichAdapterHttp,
    EnrichAdapterOpenCode,
    EnrichProvider,
)
from .input import (
    InputAdapter,
    InputAdapterClipboard,
    InputAdapterStdin,
    InputMode,
)
from .layer_shell import ensure_layer_shell_preload
from .markup import MarkdownMarkup, highlight_code
from .notify import notify
from .output import (
    OutputAdapter,
    OutputAdapterClipboard,
    OutputAdapterStdout,
    OutputAdapterType,
    OutputMode,
)
from .permissions import PermissionKind, PermissionState, normalise_tool_name
from .prompts import load_prompt, load_relative_file
from .tool_format import format_tool_args, format_tool_args_md
from .waybar import signal_waybar

_OVERLAY_EXPORTS = frozenset(
    {
        "ButtonVariant",
        "CommandPalette",
        "CommandPaletteEntry",
        "Header",
        "LayerOverlayWindow",
        "PillVariant",
        "focused_gdk_monitor",
        "focused_monitor_name",
        "load_css_from_path",
        "load_overlay_css",
        "make_button",
        "make_card",
        "make_collapsible",
        "make_pill",
    }
)

def __getattr__(name: str):
    if name in _OVERLAY_EXPORTS:
        from . import overlay as _overlay_module

        return getattr(_overlay_module, name)
    raise AttributeError(f"module 'lib' has no attribute {name!r}")

__all__ = [
    "ButtonVariant",
    "CommandPalette",
    "CommandPaletteEntry",
    "ConversationAdapter",
    "ConversationAdapterClaude",
    "ConversationAdapterOpenCode",
    "ConversationProvider",
    "DEFAULT_CONVERSE_ADAPTER",
    "DEFAULT_ENRICH_ADAPTER",
    "DEFAULT_SERVER_NAMES",
    "DEFAULT_SERVERS",
    "EnrichAdapter",
    "EnrichAdapterClaude",
    "EnrichAdapterHttp",
    "EnrichAdapterOpenCode",
    "EnrichProvider",
    "Header",
    "InputAdapter",
    "InputAdapterClipboard",
    "InputAdapterStdin",
    "InputMode",
    "LayerOverlayWindow",
    "MarkdownMarkup",
    "OutputAdapter",
    "OutputAdapterClipboard",
    "OutputAdapterStdout",
    "OutputAdapterType",
    "OutputMode",
    "PermissionKind",
    "PermissionState",
    "PillVariant",
    "PlanChunk",
    "PromptAttachment",
    "ThinkingChunk",
    "ToolCall",
    "image_attachment",
    "ensure_layer_shell_preload",
    "focused_gdk_monitor",
    "focused_monitor_name",
    "format_tool_args",
    "format_tool_args_md",
    "get_permission_seeds",
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
    "normalise_tool_name",
    "notify",
    "signal_waybar",
]
