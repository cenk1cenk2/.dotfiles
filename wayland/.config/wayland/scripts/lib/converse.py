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

from .acp_adapter import (  # noqa: F401
    AcpAdapter,
    PromptAttachment,
    build_mcp_servers,
    image_attachment,
)

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


@dataclass
class PlanChunk:
    """A snapshot of the agent's current plan. ACP agents re-emit the
    full plan each time an entry's status changes; consumers should
    replace-not-append. Each item carries `content`, `status`
    (pending/in_progress/completed), and `priority` (low/medium/high).

    Shape mirrors CodeCompanion's `on_plan` handler (PR #3008) so the
    UI semantics stay portable between clients."""

    items: list


TurnChunk = Union[str, ToolCall, ThinkingChunk, PlanChunk]

class ConversationAdapter(Protocol):
    """Streaming, stateful AI backend. Each `turn()` extends the session."""

    provider: ConversationProvider
    model: str

    def turn(
        self,
        user_message: str,
        *,
        attachments: list[PromptAttachment] | None = None,
    ) -> Iterator[TurnChunk]: ...

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
    if kind == "plan":
        return PlanChunk(items=list(payload))
    return None

class _AcpConverseAdapter(AcpAdapter):
    """Common base: adds `set_permission_handler` passthrough and
    converts the raw `(kind, payload)` tuples into `TurnChunk`."""

    provider: ConversationProvider

    def turn(
        self,
        user_message: str,
        *,
        attachments: list[PromptAttachment] | None = None,
    ) -> Iterator[TurnChunk]:
        for kind, payload in super().turn(user_message, attachments=attachments):
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
        # `system_prompt` rides on the first `session/prompt` turn as an
        # `<SYSTEM_AGENTS>`-fenced prefix — claude-agent-acp doesn't read
        # a CLAUDE_SYSTEM_PROMPT env var (we used to set one, it was a
        # no-op that silently dropped every AGENTS.md injection), and
        # the ACP `new_session` schema has no system-prompt slot. The
        # first-turn prefix is the portable path that both Claude Code
        # and OpenCode agents honour.
        self.system_prompt = system_prompt
        self.model = kwargs.get("model") or "sonnet"
        self.mode = kwargs.get("mode")
        env = dict(kwargs.get("env") or os.environ)
        env.setdefault("ANTHROPIC_MODEL", self.model)
        if self.mode:
            env["CLAUDE_PERMISSION_MODE"] = self.mode
        kwargs["env"] = env
        kwargs["command"] = kwargs.get("command") or self.DEFAULT_COMMAND
        kwargs["args"] = list(kwargs.get("args") or self.DEFAULT_ARGS)
        kwargs["client_name"] = kwargs.get("client_name") or "pilot-claude"
        kwargs["agents_file"] = self.system_prompt
        log.info(
            "ConversationAdapterClaude: model=%s mode=%s cwd=%s prefix_len=%d",
            self.model,
            self.mode,
            kwargs.get("cwd"),
            len(self.system_prompt or ""),
        )
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
        # See `ConversationAdapterClaude` — same story: the
        # `OPENCODE_SYSTEM_PROMPT` env var isn't honoured by opencode,
        # so we deliver `system_prompt` as a first-turn prefix instead.
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
        kwargs["env"] = env
        kwargs["command"] = kwargs.get("command") or self.DEFAULT_COMMAND
        kwargs["args"] = list(kwargs.get("args") or self.DEFAULT_ARGS)
        kwargs["client_name"] = kwargs.get("client_name") or "pilot-opencode"
        kwargs["agents_file"] = self.system_prompt
        log.info(
            "ConversationAdapterOpenCode: model=%s config=%s cwd=%s prefix_len=%d",
            self.model,
            self.config_path if os.path.exists(self.config_path) else None,
            kwargs.get("cwd"),
            len(self.system_prompt or ""),
        )
        super().__init__(**kwargs)
