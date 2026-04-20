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
from typing import Any, Iterator, Optional, Protocol, Union

from .acp_adapter import (  # noqa: F401
    AcpAdapter,
    PromptAttachment,
    build_mcp_servers,
    image_attachment,
)
from .tools import ToolFormatters

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
    execution through the ACP permission flow.

    `name` is the canonical programmatic tool name (used for permission
    set membership + formatter dispatch). `title` is the human-readable
    header the agent wants shown — often identical to `name` but for
    Claude turns into `"Read README.md"` / `"$ ls -la"`. `kind` is the
    ACP `ToolKind` enum when the agent supplies one, empty otherwise."""

    tool_id: str
    name: str
    arguments: str
    status: str = "completed"
    audit: bool = False
    title: str = ""
    kind: str = ""

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

    def set_permission_handler(self, handler: Any) -> None: ...

    def reset(self) -> None: ...

    @property
    def mcp_server_names(self) -> list[str]: ...

    @property
    def session_id(self) -> str | None: ...

    @property
    def session_resumed(self) -> bool: ...

    @property
    def session_store_path(self) -> str | None: ...

    @property
    def tool_formatters(self) -> ToolFormatters:
        """Per-adapter tool-formatter instance. Consumers call
        `adapter.tool_formatters.format(name, args)` to render a
        tool-call; each adapter returns an instance of its chosen
        subclass of `ToolFormatters` so the UI doesn't need to know
        which backend produced the call."""
        ...

class _AcpConverseAdapter(AcpAdapter):
    """Common base: adds `set_permission_handler` passthrough, owns
    a per-adapter `ToolFormatters` instance, and converts the raw
    ACP `(kind, payload)` tuples into `TurnChunk`."""

    provider: ConversationProvider

    # Subclasses override this with a ToolFormatters subclass to get
    # per-adapter rendering. The default (shared with Claude) is the
    # class itself — Claude's SDK tool shapes are what
    # `ToolFormatters` was modelled on.
    FORMATTERS_CLASS: type[ToolFormatters] = ToolFormatters

    def __init__(self, **kwargs: Any):
        super().__init__(**kwargs)
        # One formatter instance per adapter instance. Built lazily
        # from `FORMATTERS_CLASS` so subclasses only have to set the
        # class attribute and get the right variant automatically.
        self._tool_formatters: ToolFormatters = self.FORMATTERS_CLASS()

    @property
    def tool_formatters(self) -> ToolFormatters:
        return self._tool_formatters

    @staticmethod
    def _translate_acp_chunk(kind: str, payload: Any) -> TurnChunk | None:
        """Fold an `AcpAdapter.iter_events()` tuple into the
        `TurnChunk` union. Staticmethod because it's pure data
        massage with no adapter state — every subclass's `turn()`
        calls it."""
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
                title=payload.title,
                kind=payload.kind,
            )
        if kind == "plan":
            return PlanChunk(items=list(payload))
        return None

    def turn(
        self,
        user_message: str,
        *,
        attachments: list[PromptAttachment] | None = None,
    ) -> Iterator[TurnChunk]:
        for kind, payload in self.iter_events(user_message, attachments=attachments):
            chunk = self._translate_acp_chunk(kind, payload)
            if chunk is not None:
                yield chunk


class ConversationAdapterClaude(_AcpConverseAdapter):
    """Claude Code via `bunx @agentclientprotocol/claude-agent-acp`.

    Auth follows the Claude Agent SDK (`ANTHROPIC_API_KEY` env, keychain).
    Permission prompts flow through ACP's `session/request_permission`."""

    provider = ConversationProvider.CLAUDE

    DEFAULT_COMMAND = "bunx"
    DEFAULT_ARGS: tuple[str, ...] = ("--bun", "@agentclientprotocol/claude-agent-acp")

    # Claude's tool shapes are what the default registry was modelled
    # on, so there's nothing to override. The inherited
    # `_AcpConverseAdapter.tool_formatters` is fine as-is.

    @staticmethod
    def _tool_name_from_meta(update: Any) -> Optional[str]:
        """Pull `_meta.claudeCode.toolName` off an ACP update when
        Claude's `claude-agent-acp` ships it. That's the ONLY channel
        carrying the original Anthropic tool name (`Bash` / `Read` /
        `mcp__server__tool`) through the protocol — `title` has been
        mutated into human prose and `kind` is coerced to the
        `ToolKind` enum. Returns None when the field is absent so the
        shared transport layer falls back to title / kind.

        Lives on this class (not `AcpSession` / `acp_adapter`) because
        it's branded Claude knowledge — opencode and any future ACP
        agent don't emit this `_meta.claudeCode` envelope."""
        meta = getattr(update, "field_meta", None)
        if not isinstance(meta, dict):
            return None
        cc = meta.get("claudeCode")
        if not isinstance(cc, dict):
            return None
        name = cc.get("toolName")
        if isinstance(name, str) and name.strip():
            return name.strip()
        return None

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
        # Claude tucks the SDK tool name into `_meta.claudeCode.toolName`
        # on session updates. Register the extractor here so the shared
        # ACP transport layer stays brand-agnostic.
        self.set_tool_name_extractor(self._tool_name_from_meta)

class OpenCodeToolFormatters(ToolFormatters):
    """OpenCode-flavoured tool formatters.

    The `ToolFormatters` baseline already absorbs most of opencode's
    camelCase drift via `_pop_str` synonym lists. This subclass adds
    the genuinely-different tools — `codesearch`, `lsp`, `skill`,
    `question`, `external_directory` — and registers them (plus one
    spelling alias) on top of the defaults.

    Subclassing pattern: override `_seed_defaults` to call
    `super()._seed_defaults()` first, then `register` new formatters
    and `alias` any alt-spellings. Formatters themselves are just
    methods — use `self._pop_str` / `self._pop` / `self._fence` /
    `self._truncate` for the shared primitives."""

    def _seed_defaults(self) -> None:
        super()._seed_defaults()
        self.register("codesearch", self.format_codesearch)
        self.register("lsp", self.format_lsp)
        self.register("skill", self.format_skill)
        self.register("question", self.format_question)
        self.register("external_directory", self.format_external_directory)
        # opencode's ACP agent sometimes shortens the permission key
        # without the underscore; routing both spellings at the map
        # level keeps the formatter single-source.
        self.alias("externaldirectory", "external_directory")

    def format_codesearch(self, args: dict) -> str:
        query = self._pop_str(args, "query")
        tokens = self._pop(args, "tokensNum")
        header = f"🔎 **codesearch** `{query}`" if query else "🔎 **codesearch**"
        if isinstance(tokens, (int, float)) and tokens:
            header += f"  *(tokens={int(tokens)})*"
        return header

    def format_lsp(self, args: dict) -> str:
        operation = self._pop_str(args, "operation")
        path = self._pop_str(args, "filePath", "file_path", "path")
        line = self._pop(args, "line")
        character = self._pop(args, "character")
        bits = ["🧭 **lsp**"]
        if operation:
            bits.append(f"`{operation}`")
        if path:
            location = path
            if isinstance(line, int):
                location += f":{line}"
                if isinstance(character, int):
                    location += f":{character}"
            bits.append(f"`{location}`")
        elif isinstance(line, int):
            bits.append(f"line `{line}`")
        return "  ".join(bits)

    def format_skill(self, args: dict) -> str:
        # Opencode's own `skill` tool takes a single `name` (the
        # skill id). Distinct from pilot's `mcp__pilot__load_skill`,
        # which does the same job via a different route — we keep
        # both since they ride different dispatch buses.
        name = self._pop_str(args, "name")
        return f"🧠 **skill** `{name}`" if name else "🧠 skill"

    def format_question(self, args: dict) -> str:
        # Opencode emits `questions: [{question, options[], ...}, …]`
        # — an array even when there's a single prompt. Render each
        # as a blockquote so the user sees exactly what the agent is
        # asking; inline option labels when the agent restricted
        # answers to a closed set.
        questions = self._pop(args, "questions") or []
        if not isinstance(questions, list) or not questions:
            return "❓ **question**"
        parts = ["❓ **question**"]
        for idx, q in enumerate(questions[:3]):
            if not isinstance(q, dict):
                continue
            prompt = self._pop_str(q, "question", "prompt")
            header = self._pop_str(q, "header")
            options = self._pop(q, "options") or []
            # Drop other known shape fields so they don't show up in
            # the per-question JSON dump.
            for noise in ("multiple", "custom", "_meta"):
                self._pop(q, noise)
            if not prompt:
                continue
            prefix = f"**Q{idx + 1}.**  " if len(questions) > 1 else ""
            heading = f"{prefix}{prompt}"
            if header:
                heading = f"{header} — {heading}"
            lines = [f"> {heading}"]
            if isinstance(options, list) and options:
                labels: list[str] = []
                for opt in options:
                    if isinstance(opt, str):
                        labels.append(opt)
                    elif isinstance(opt, dict):
                        label = opt.get("label") or opt.get("value")
                        if label:
                            labels.append(str(label))
                if labels:
                    lines.append(
                        "> *options:* " + " · ".join(f"`{l}`" for l in labels)
                    )
            parts.append("\n".join(lines))
        if len(questions) > 3:
            parts.append(f"*… +{len(questions) - 3} more*")
        return "\n\n".join(parts)

    def format_external_directory(self, args: dict) -> str:
        path = self._pop_str(args, "path", "filePath")
        return (
            f"📂 **external directory** `{path}`"
            if path
            else "📂 external directory"
        )


class ConversationAdapterOpenCode(_AcpConverseAdapter):
    """OpenCode via `opencode acp`.

    `opencode.json`'s `permission` block is honoured end-to-end — every
    `ask` rule pops through `session/request_permission`.

    OpenCode diverges from Claude's SDK shape: tool names are lower-
    case, arg keys camelCase, and there are opencode-only tools
    (`codesearch`, `lsp`, `skill`, `question`, `external_directory`).
    `OpenCodeToolFormatters` absorbs the differences — the adapter
    just points `FORMATTERS_CLASS` at it so
    `_AcpConverseAdapter.__init__` builds the right instance."""

    provider = ConversationProvider.OPENCODE

    DEFAULT_COMMAND = "opencode"
    DEFAULT_ARGS: tuple[str, ...] = ("acp",)
    DEFAULT_CONFIG_PATH = os.path.expanduser(
        "~/.config/nvim/utils/agents/opencode/kilic.json"
    )

    FORMATTERS_CLASS = OpenCodeToolFormatters

    def __init__(self, system_prompt: str, **kwargs: Any):
        # See `ConversationAdapterClaude` — same story: the
        # `OPENCODE_SYSTEM_PROMPT` env var isn't honoured by opencode,
        # so we deliver `system_prompt` as a first-turn prefix instead.
        self.system_prompt = system_prompt
        # `model` is Protocol-typed as `str`; coerce None → "" so an
        # unset --model flag doesn't bleed a None into the header /
        # waybar status renderers that expect a string.
        self.model: str = kwargs.get("model") or ""
        self.mode = kwargs.get("mode")
        self.provider_name = kwargs.get("provider_name") or "kilic"
        self.config_path = kwargs.get("config_path") or self.DEFAULT_CONFIG_PATH
        env = dict(kwargs.get("env") or os.environ)
        # FORCE the config + model into env, not `setdefault`. The shell
        # env frequently carries leftover `OPENCODE_MODEL` entries from
        # other opencode CLI runs; `setdefault` let that stale value
        # win against the user's `--converse-model` flag, which is why
        # `spawn plan` was silently routing to whichever model that
        # stale env pinned.
        if os.path.exists(self.config_path):
            env["OPENCODE_CONFIG"] = self.config_path
        if self.model:
            env["OPENCODE_MODEL"] = f"{self.provider_name}/{self.model}"
        kwargs["env"] = env
        kwargs["command"] = kwargs.get("command") or self.DEFAULT_COMMAND
        # DO NOT pass `--model` to `opencode acp` as a CLI flag. yargs
        # bails out with the help text and never actually starts the
        # ACP server when a top-level option appears alongside the
        # `acp` subcommand (verified empirically with
        # `opencode --model kilic/glm-5.1:cloud acp` → prints help).
        # We rely on `OPENCODE_MODEL` env (set above) + the user's
        # `opencode.json` default to pin the model instead.
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
