"""Streaming conversational AI backends.

Sibling of `EnrichAdapter`: where enrichment is a one-shot text rewrite,
these adapters hold a multi-turn session and yield response chunks as
they arrive. Both shipping adapters (`ConversationAdapterClaude`,
`ConversationAdapterOpenCode`) speak the Agent Client Protocol via
`lib.acp_adapter`."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Iterator, Protocol, Union

from .acp_adapter import AcpAdapter, build_mcp_servers  # noqa: F401

log = logging.getLogger(__name__)

class ConversationProvider(StrEnum):
    CLAUDE = "claude"
    OPENCODE = "opencode"

DEFAULT_CONVERSE_ADAPTER = ConversationProvider.CLAUDE

@dataclass
class ToolCall:
    """A tool-use event surfaced alongside text chunks.

    `status` moves `pending → running → completed`. `audit=True` means
    the event is informational (bubble strip); `audit=False` gates real
    execution through the ACP permission flow."""

    tool_id: str
    name: str
    arguments: str
    status: str = "completed"
    audit: bool = False

@dataclass
class ThinkingChunk:
    """Streamed reasoning / extended-thinking content.

    The UI renders these into a collapsible section inside the active
    assistant card; the section auto-collapses the moment the first
    regular text chunk arrives."""

    text: str

TurnChunk = Union[str, ToolCall, ThinkingChunk]

class ConversationAdapter(Protocol):
    """Streaming, stateful AI backend. Each `turn()` extends the session."""

    provider: ConversationProvider
    model: str

    def turn(self, user_message: str) -> Iterator[TurnChunk]: ...

    def cancel(self) -> None: ...

    def close(self) -> None: ...

def _translate_acp_chunk(kind: str, payload: Any) -> TurnChunk | None:
    """Fold an `AcpAdapter.turn()` tuple into the TurnChunk union."""
    if kind == "text":
        return str(payload)
    if kind == "thinking":
        return ThinkingChunk(text=str(payload))
    if kind == "tool":
        return ToolCall(
            tool_id=payload.tool_id,
            name=payload.name,
            arguments=payload.arguments,
            status=payload.status,
            audit=True,
        )
    return None

class _AcpConverseAdapter(AcpAdapter):
    """Common base: adds `set_permission_handler` passthrough and
    converts the raw `(kind, payload)` tuples into `TurnChunk`."""

    provider: ConversationProvider

    def turn(self, user_message: str) -> Iterator[TurnChunk]:
        for kind, payload in super().turn(user_message):
            chunk = _translate_acp_chunk(kind, payload)
            if chunk is not None:
                yield chunk

class ConversationAdapterClaude(_AcpConverseAdapter):
    """Claude Code via `bunx @agentclientprotocol/claude-agent-acp`.

    Auth follows the Claude Agent SDK (`ANTHROPIC_API_KEY` env, keychain).
    Permission prompts flow through ACP's `session/request_permission`."""

    provider = ConversationProvider.CLAUDE

    DEFAULT_COMMAND = "bunx"
    DEFAULT_ARGS: tuple[str, ...] = ("--bun", "@agentclientprotocol/claude-agent-acp")

    def __init__(self, system_prompt: str, **kwargs: Any):
        self.system_prompt = system_prompt
        self.model = kwargs.get("model") or "sonnet"
        self.mode = kwargs.get("mode")
        env = dict(kwargs.get("env") or os.environ)
        env.setdefault("ANTHROPIC_MODEL", self.model)
        if self.mode:
            env["CLAUDE_PERMISSION_MODE"] = self.mode
        if self.system_prompt:
            env["CLAUDE_SYSTEM_PROMPT"] = self.system_prompt
        kwargs["env"] = env
        kwargs["command"] = kwargs.get("command") or self.DEFAULT_COMMAND
        kwargs["args"] = list(kwargs.get("args") or self.DEFAULT_ARGS)
        kwargs["client_name"] = kwargs.get("client_name") or "pilot-claude"
        super().__init__(**kwargs)

class ConversationAdapterOpenCode(_AcpConverseAdapter):
    """OpenCode via `opencode acp`.

    `opencode.json`'s `permission` block is honoured end-to-end — every
    `ask` rule pops through `session/request_permission`."""

    provider = ConversationProvider.OPENCODE

    DEFAULT_COMMAND = "opencode"
    DEFAULT_ARGS: tuple[str, ...] = ("acp",)
    DEFAULT_CONFIG_PATH = os.path.expanduser(
        "~/.config/nvim/utils/agents/opencode/kilic.json"
    )

    def __init__(self, system_prompt: str, **kwargs: Any):
        self.system_prompt = system_prompt
        self.model = kwargs.get("model")
        self.mode = kwargs.get("mode")
        self.provider_name = kwargs.get("provider_name") or "kilic"
        self.config_path = kwargs.get("config_path") or self.DEFAULT_CONFIG_PATH
        env = dict(kwargs.get("env") or os.environ)
        if os.path.exists(self.config_path):
            env.setdefault("OPENCODE_CONFIG", self.config_path)
        if self.model:
            env.setdefault("OPENCODE_MODEL", f"{self.provider_name}/{self.model}")
        if self.system_prompt:
            env.setdefault("OPENCODE_SYSTEM_PROMPT", self.system_prompt)
        kwargs["env"] = env
        kwargs["command"] = kwargs.get("command") or self.DEFAULT_COMMAND
        kwargs["args"] = list(kwargs.get("args") or self.DEFAULT_ARGS)
        kwargs["client_name"] = kwargs.get("client_name") or "pilot-opencode"
        super().__init__(**kwargs)
