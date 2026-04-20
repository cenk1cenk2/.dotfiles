"""Agent Client Protocol (ACP) transport for the conversation adapters.

ACP is a stdio JSON-RPC protocol between a client (us) and an agent
subprocess. We use it as the transport for backends that speak it
natively — `opencode acp` and `bunx @agentclientprotocol/claude-agent-acp`
today — because `session/request_permission` is a real blocking
round-trip: the agent halts until our client replies, mirroring the
MCP `--permission-prompt-tool` pattern but built into the protocol
itself.

This module is backend-agnostic scaffolding: the client class bridges
streaming + permission callbacks to caller-supplied sinks, the session
owns the asyncio loop + agent subprocess, and the adapter wraps them
behind the `turn() -> Iterator[TurnChunk]` contract the rest of
`lib.converse` exposes.

Caller wiring (e.g. `pilot.py`) is expected to:
  - instantiate `ConversationAdapterClaude` / `ConversationAdapterOpenCode`
    from `lib.converse` (both subclass `AcpAdapter` here), and
  - call `adapter.set_permission_handler(fn)` before the first turn to
    route `session/request_permission` into the overlay UI.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import threading
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass
from typing import Any, Callable, ClassVar, Iterator, Optional

import acp
from acp import (
    Client,
    ClientSideConnection,
    PROTOCOL_VERSION,
    RequestPermissionResponse,
    image_block,
    spawn_agent_process,
    text_block,
)
import base64
from acp.schema import (
    AgentMessageChunk,
    AgentPlanUpdate,
    AgentThoughtChunk,
    AllowedOutcome,
    AuthCapabilities,
    ClientCapabilities,
    DeniedOutcome,
    EnvVariable,
    FileSystemCapabilities,
    HttpHeader,
    HttpMcpServer,
    Implementation,
    McpServerStdio,
    PermissionOption,
    SseMcpServer,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
    ToolCallUpdate,
    UserMessageChunk,
)

log = logging.getLogger(__name__)

# Preference order per PermissionRow action. When the agent didn't ship
# the exact kind we'd like (opencode offers only `allow_once / allow_always
# / reject_once`, no `reject_always`), fall through to the closest match.
# Module-level because `AcpAdapter.select_option_id` below is a
# staticmethod that has to read it without an instance — moving it onto
# the class as a ClassVar works too but buys nothing; it's a tiny lookup
# table, not a policy to override per-adapter.
_KIND_FALLBACK: dict[str, tuple[str, ...]] = {
    "allow_once": ("allow_once", "allow_always"),
    "allow_always": ("allow_always", "allow_once"),
    "reject_once": ("reject_once", "reject_always"),
    "reject_always": ("reject_always", "reject_once"),
}

AcpMcpServer = HttpMcpServer | SseMcpServer | McpServerStdio

ToolNameExtractor = Callable[[Any], Optional[str]]
"""Hook for agent-specific canonical tool-name extraction.

Gets handed a `ToolCallUpdate` / `ToolCallStart` / `ToolCallProgress`
and returns the programmatic tool name if the agent surfaced one
through an out-of-band channel (Claude ships it in
`_meta.claudeCode.toolName`), or None to fall back to the ACP
`title` / `kind` fields. Wired onto `AcpSession` / `AcpAdapter` so
backend-specific logic (like the Claude hook) lives in the adapter
that owns it, not in the transport core."""


@dataclass(frozen=True)
class PromptAttachment:
    """A non-text payload to prepend to a prompt. Used today for
    pasted images; audio / arbitrary blobs fit the same shape. Either
    `data` (raw bytes) OR `uri` + `mime_type` must be set — the
    adapter converts into the appropriate ACP content block at
    submit-time."""

    mime_type: str
    data: Optional[bytes] = None
    uri: Optional[str] = None

    @classmethod
    def image(
        cls, data: bytes, mime_type: str = "image/png"
    ) -> PromptAttachment:
        """Shorthand for an inline-bytes image attachment. Lives on
        the dataclass so tests + callers can build one without
        fishing through the module for a free-standing helper."""
        return cls(mime_type=mime_type, data=data)


def image_attachment(data: bytes, mime_type: str = "image/png") -> PromptAttachment:
    """Back-compat free function — forwards to
    `PromptAttachment.image`. Kept so existing
    `from lib import image_attachment` imports don't break."""
    return PromptAttachment.image(data, mime_type)


@dataclass(frozen=True)
class PlanItem:
    """One entry in an `AgentPlanUpdate`. Agents emit a fresh plan
    list on every `notify::plan` — we re-render the whole thing each
    time rather than diffing."""

    content: str
    status: str  # "pending" | "in_progress" | "completed"
    priority: str  # "low" | "medium" | "high"


@dataclass(frozen=True)
class ToolCallSummary:
    """Lightweight snapshot of an ACP ToolCallUpdate shaped for the
    permission-handler callback.

    `name` is the canonical programmatic tool name used for permission
    set membership and formatter dispatch. `title` is the human-readable
    line the agent wants surfaced in UI, and `kind` is the ACP `ToolKind`
    enum (`read` / `edit` / `execute` / `search` / `fetch` / …).

    The split matters because ACP's `ToolCall` / `ToolCallUpdate` carry
    only `title` + `kind` on the wire. Claude's `claude-agent-acp` tucks
    the real tool name into `_meta.claudeCode.toolName` on session
    updates; opencode puts its lowercase permission category in `title`
    directly. We pull whichever is most accurate into `name` so trust /
    auto-approve decisions survive across calls with varying titles."""

    # ACP `status` values → the pilot UI's compact vocabulary. Lives
    # on the dataclass because `from_acp_update` is the sole consumer.
    _STATUS_MAP: ClassVar[dict[str, str]] = {
        "pending": "pending",
        "in_progress": "running",
        "completed": "completed",
        "failed": "failed",
    }

    tool_id: str
    name: str
    title: str
    kind: str
    arguments: str
    status: str

    @classmethod
    def from_acp_update(
        cls,
        update: ToolCallUpdate | ToolCallStart | ToolCallProgress,
        *,
        tool_name_extractor: Optional[ToolNameExtractor] = None,
    ) -> ToolCallSummary:
        """Collapse an ACP tool_call message into the pilot-side
        summary. Backend-specific `tool_name_extractor` (see Claude's
        `_meta.claudeCode.toolName` hook) gets first pick at the
        canonical tool name; we fall through to `title` and `kind` so
        there's always something stable to hand to the permission
        engine."""
        tool_id = update.tool_call_id or ""
        title = (update.title or "").strip()
        kind = (update.kind or "").strip()
        name: Optional[str] = None
        if tool_name_extractor is not None:
            try:
                name = tool_name_extractor(update)
            except Exception as e:
                log.warning("tool_name_extractor raised: %s", e)
                name = None
        if not name:
            name = title or kind or "tool"
        raw = update.raw_input
        if isinstance(raw, (dict, list)):
            try:
                args = json.dumps(raw)
            except TypeError, ValueError:
                args = str(raw)
        elif raw is None:
            args = ""
        else:
            args = str(raw)
        return cls(
            tool_id=tool_id,
            name=str(name),
            title=title or str(name),
            kind=kind,
            arguments=args,
            status=cls._STATUS_MAP.get(update.status or "", "pending"),
        )


PermissionHandler = Callable[[ToolCallSummary, list[PermissionOption]], Optional[str]]
"""Invoked from the ACP worker thread when the agent asks for
permission. MUST block until the user decides and return the
`option_id` to send back — or None to cancel the prompt."""

def build_mcp_servers(
    servers: dict[str, dict] | None,
) -> list[AcpMcpServer]:
    """Translate an `{name: spec}` dict into the typed ACP
    `new_session.mcp_servers` payload. `spec` is either:

      - stdio: `{command, args?, env?}`
      - http / sse: `{type: "http"|"sse", url, headers?}`

    Empty / missing → empty list so adapters can pass `None` without
    branching."""
    if not servers:
        return []
    out: list[AcpMcpServer] = []
    for name, spec in servers.items():
        if not name:
            continue
        if spec.get("command"):
            env_list = [
                EnvVariable(name=k, value=str(v))
                for k, v in (spec.get("env") or {}).items()
            ]
            out.append(
                McpServerStdio(
                    name=name,
                    command=spec["command"],
                    args=list(spec.get("args") or []),
                    env=env_list,
                )
            )
            continue
        url = spec.get("url")
        if not url:
            log.warning("skipping mcp server %r: no command or url", name)
            continue
        header_list = [
            HttpHeader(name=k, value=str(v))
            for k, v in (spec.get("headers") or {}).items()
        ]
        stype = (spec.get("type") or "http").lower()
        if stype == "sse":
            out.append(
                SseMcpServer(name=name, url=url, headers=header_list, type="sse")
            )
        else:
            out.append(
                HttpMcpServer(name=name, url=url, headers=header_list, type="http")
            )
    return out

@dataclass
class _Sentinel:
    """Pushed onto the session queue when the in-flight prompt resolves
    (successfully or not). Drains the adapter's generator."""

    error: Optional[BaseException] = None

class AcpClient(Client):
    """Backend-agnostic ACP client. Pushes streaming events onto a
    thread-safe queue as raw tuples so the adapter can translate them
    into its own chunk type without this module having to know about
    `ToolCall` / `ThinkingChunk` (avoids a cycle with `lib.converse`).

    The queue reference is looked up per-event through
    `queue_lookup()` — the session swaps the active queue between
    turns, so a single long-lived client can drain events for every
    turn in the session instead of getting stuck on the first one."""

    def __init__(
        self,
        queue_lookup: Callable[[], Optional[queue.Queue[tuple[str, Any] | _Sentinel]]],
        permission_handler_lookup: Callable[[], Optional[PermissionHandler]],
        tool_name_extractor_lookup: Callable[
            [], Optional[ToolNameExtractor]
        ] = lambda: None,
    ):
        self._queue_lookup = queue_lookup
        self._permission_handler_lookup = permission_handler_lookup
        self._tool_name_extractor_lookup = tool_name_extractor_lookup

    @staticmethod
    def _content_text(content: Any) -> Optional[str]:
        """ACP content blocks are a discriminated union; we only
        surface the text variant (chat + thought chunks). Lives here
        (not at module scope) because `session_update` is the only
        caller — embedding keeps the acp-content-envelope logic next
        to the place it's unpacked."""
        if isinstance(content, TextContentBlock):
            return content.text or None
        text = getattr(content, "text", None)
        if isinstance(text, str):
            return text or None
        return None

    def _summarise(self, update: Any) -> ToolCallSummary:
        """Build a `ToolCallSummary` using the adapter's current tool-
        name extractor. Thin wrapper so `session_update` and
        `request_permission` don't each duplicate the extractor
        lookup."""
        return ToolCallSummary.from_acp_update(
            update, tool_name_extractor=self._tool_name_extractor_lookup()
        )

    def _put(self, item: tuple[str, Any] | _Sentinel) -> None:
        q = self._queue_lookup()
        if q is not None:
            q.put(item)

    async def session_update(self, session_id: str, update: Any, **_: Any) -> None:
        try:
            if isinstance(update, AgentMessageChunk):
                text = self._content_text(update.content)
                if text:
                    log.debug("acp update: text chunk len=%d", len(text))
                    self._put(("text", text))
            elif isinstance(update, AgentThoughtChunk):
                text = self._content_text(update.content)
                if text:
                    log.debug("acp update: thinking chunk len=%d", len(text))
                    self._put(("thinking", text))
            elif isinstance(update, (ToolCallStart, ToolCallProgress)):
                summary = self._summarise(update)
                log.info(
                    "acp update: tool %s name=%s status=%s",
                    "start" if isinstance(update, ToolCallStart) else "progress",
                    summary.name,
                    summary.status,
                )
                self._put(("tool", summary))
            elif isinstance(update, AgentPlanUpdate):
                items = [
                    PlanItem(
                        content=str(e.content or "").strip() or "Untitled",
                        status=str(e.status or "pending"),
                        priority=str(e.priority or "medium"),
                    )
                    for e in (update.entries or [])
                ]
                if items:
                    log.info("acp update: plan entries=%d", len(items))
                    self._put(("plan", items))
            elif isinstance(update, UserMessageChunk):
                return
            else:
                log.debug("acp update: dropped kind=%s", type(update).__name__)
            # Usage / mode updates intentionally dropped.
        except Exception as e:  # pragma: no cover — defensive
            log.warning("session_update dispatch failed: %s", e)

    async def request_permission(
        self,
        options: list[PermissionOption],
        session_id: str,
        tool_call: ToolCallUpdate,
        **_: Any,
    ) -> RequestPermissionResponse:
        handler = self._permission_handler_lookup()
        summary = self._summarise(tool_call)
        kinds = [opt.kind for opt in options if opt.kind]
        log.info(
            "acp request_permission: tool=%s options=%s",
            summary.name,
            kinds,
        )
        if handler is None:
            log.warning("no permission handler set; cancelling ACP prompt")
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        loop = asyncio.get_running_loop()
        try:
            option_id = await loop.run_in_executor(None, handler, summary, options)
        except Exception as e:
            log.warning("permission handler raised: %s", e)
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        if not option_id:
            log.info("acp request_permission: user cancelled tool=%s", summary.name)
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        log.info(
            "acp request_permission: user picked option_id=%s tool=%s",
            option_id,
            summary.name,
        )
        return RequestPermissionResponse(
            outcome=AllowedOutcome(outcome="selected", option_id=option_id)
        )

    # ── filesystem stubs ──────────────────────────────────────────
    # We advertise fs read/write capability on `initialize`, so agents
    # that want to edit through the client (claude-agent-acp does)
    # call into these. Pilot runs in the same sandbox as the agent, so
    # direct disk I/O is fine.

    async def read_text_file(
        self,
        path: str,
        session_id: str,
        limit: int | None = None,
        line: int | None = None,
        **_: Any,
    ) -> acp.ReadTextFileResponse:
        try:
            with open(path, "r", encoding="utf-8") as f:
                if line is None and limit is None:
                    content = f.read()
                else:
                    lines = f.readlines()
                    start = (line or 1) - 1
                    lines = (
                        lines[start : start + limit]
                        if limit is not None
                        else lines[start:]
                    )
                    content = "".join(lines)
        except OSError as e:
            raise acp.RequestError.internal_error({"message": str(e)})
        return acp.ReadTextFileResponse(content=content)

    async def write_text_file(
        self,
        content: str,
        path: str,
        session_id: str,
        **_: Any,
    ) -> Optional[acp.WriteTextFileResponse]:
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        except OSError as e:
            raise acp.RequestError.internal_error({"message": str(e)})
        return None

class AcpSession:
    """Owns the asyncio loop, the spawned agent subprocess, and the ACP
    session. Reused across every turn on an adapter so the agent sees
    one continuous conversation rather than per-turn cold starts.

    Config via `**kwargs`:
      - `command`: executable to spawn (required)
      - `args`: list of argv strings appended to `command`
      - `cwd`: working directory for the subprocess
      - `env`: environment dict (full — caller merges with os.environ)
      - `client_name` / `client_version`: reported on `initialize`
      - `mcp_servers`: iterable of ACP `McpServer*` dataclasses OR an
        `{name: spec}` dict (we convert via `build_mcp_servers`). These
        are handed to the agent at `new_session` so claude-agent-acp /
        opencode-acp can spin them up on the agent side.
    """

    # Raises asyncio's default 64KB line-buffer ceiling. ACP agents
    # emit newline-delimited JSON-RPC frames that routinely cross the
    # default on turns that stream a tool with a large `raw_input` /
    # `raw_output` (file reads, image blocks already base64-encoded,
    # long diff patches), and the SDK surfaces that as
    # `LimitOverrunError` inside the receive loop — which tears the
    # connection down and leaves the UI stuck on a half-delivered
    # reply. Anthropic's streaming payloads stay well under 16MB per
    # line, so bumping the ceiling to that buys headroom without
    # making us buffer anything we wouldn't have already.
    STREAM_LIMIT_BYTES: ClassVar[int] = 16 * 1024 * 1024

    # Cap the size of a single raw-wire log line so a 10MB edit
    # payload doesn't flood the terminal when running `pilot -v`.
    # Still enough to see a full permission envelope.
    WIRE_LOG_LIMIT: ClassVar[int] = 4096

    @staticmethod
    def _build_prompt_blocks(
        user_message: str, attachments: list[PromptAttachment]
    ) -> list:
        """Compose the ACP prompt payload. Attachments prefix the text
        so their content is visible BEFORE the prose (matches how
        agents typically quote images in responses). A text block
        always comes out last — even an empty prose turn keeps the
        content array non-empty which the ACP spec requires."""
        blocks: list = []
        for att in attachments:
            if att.data is not None:
                blocks.append(
                    image_block(
                        data=base64.b64encode(att.data).decode("ascii"),
                        mime_type=att.mime_type,
                        uri=att.uri,
                    )
                )
            elif att.uri is not None:
                # No inline bytes; reference the URI directly. `uri`
                # alone on an image_block is the "server-resolved"
                # shape from MCP `resources/read` results.
                blocks.append(
                    image_block(data="", mime_type=att.mime_type, uri=att.uri)
                )
        blocks.append(text_block(user_message))
        return blocks

    @classmethod
    def _summarise_wire_payload(cls, message: dict) -> str:
        """Render a JSON-RPC message (request / response /
        notification) as a compact debug line. Keeps method + id on
        the prefix and a best-effort dump of the params/result
        payload — truncated at `WIRE_LOG_LIMIT` so one huge payload
        doesn't wedge the logger."""
        method = message.get("method")
        msg_id = message.get("id")
        if method is not None:
            prefix = f"{method}"
            if msg_id is not None:
                prefix += f" id={msg_id}"
            body = message.get("params")
        else:
            prefix = f"response id={msg_id}"
            body = (
                message.get("result")
                if "result" in message
                else message.get("error")
            )
        try:
            dumped = json.dumps(body, ensure_ascii=False, default=str)
        except TypeError, ValueError:
            dumped = repr(body)
        if len(dumped) > cls.WIRE_LOG_LIMIT:
            dumped = (
                dumped[: cls.WIRE_LOG_LIMIT]
                + f"…(+{len(dumped) - cls.WIRE_LOG_LIMIT})"
            )
        return f"{prefix}  {dumped}"

    @classmethod
    def _make_wire_observer(cls):
        """Return a `StreamObserver` (from `acp.connection`) that logs
        every inbound / outbound JSON-RPC frame at DEBUG. Returns
        synchronously so the observer doesn't queue extra coroutines
        on the hot path."""
        from acp.connection import StreamDirection

        summarise = cls._summarise_wire_payload

        def observer(event) -> None:
            if not log.isEnabledFor(logging.DEBUG):
                return
            arrow = "←" if event.direction == StreamDirection.INCOMING else "→"
            try:
                line = summarise(event.message)
            except Exception as e:
                line = f"(wire log failed: {e}) {event.message!r}"
            log.debug("acp wire %s %s", arrow, line)

        return observer

    @staticmethod
    async def _drain_stderr(stream, tag: str) -> None:
        """Forward each line of the ACP agent's stderr to our logger
        at DEBUG. `tag` is the executable basename so the user can
        tell claude-agent-acp output apart from opencode's when
        multiple subprocesses are alive. Silently exits once the
        stream closes (subprocess died or stderr was redirected)."""
        try:
            while True:
                line = await stream.readline()
                if not line:
                    return
                text = line.rstrip(b"\n").decode("utf-8", errors="replace")
                if text:
                    log.debug("acp agent[%s] stderr: %s", tag, text)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.debug("acp stderr drain for %s ended: %s", tag, e)

    def __init__(self, **kwargs: Any):
        self.command: str = kwargs.get("command") or ""
        if not self.command:
            raise ValueError("AcpSession requires `command`")
        self.args: list[str] = list(kwargs.get("args") or ())
        self.cwd: Optional[str] = kwargs.get("cwd")
        self.env: Optional[dict[str, str]] = kwargs.get("env")
        self.client_name: str = kwargs.get("client_name") or "acp-client"
        self.client_version: str = kwargs.get("client_version") or "1.0"
        raw_mcp = kwargs.get("mcp_servers")
        if raw_mcp is None:
            self.mcp_servers: list[AcpMcpServer] = []
        elif isinstance(raw_mcp, dict):
            self.mcp_servers = build_mcp_servers(raw_mcp)
        else:
            self.mcp_servers = list(raw_mcp)
        # Optional AGENTS.md / system-instruction blob that rides on the
        # FIRST prompt of the session and then gets cleared. Claude /
        # OpenCode CLIs don't honour the env-var injection we used to
        # try, so delivering this via a user-message prefix is the
        # portable path. Consumed + nulled inside `prompt()`.
        raw_prefix = kwargs.get("agents_file")
        # `_original_agents_file` is the immutable reference we read on
        # `reset()` to re-arm the prefix when spinning up a replacement
        # session inside the same adapter — without it, a Ctrl+S
        # "new session" would skip the AGENTS.md injection because
        # `_agents_file` was already consumed by the previous session's
        # first prompt.
        self._original_agents_file: str = (raw_prefix or "").strip()
        self._agents_file: str = self._original_agents_file
        # Optional path to a plain-text file that holds the last ACP
        # `session_id` this adapter obtained. When set, `_ensure_started`
        # tries `conn.load_session(session_id, ...)` first — if the agent
        # still has it, the conversation resumes from wherever it left
        # off. On failure (agent forgot / file is stale / first launch)
        # we fall through to `new_session` and overwrite the store with
        # whatever id comes back. `close()` intentionally does NOT call
        # `conn.close_session` — we want the session to outlive the
        # subprocess so the NEXT launch can pick it back up.
        self.session_store_path: Optional[str] = kwargs.get("session_store_path")

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._conn: Optional[ClientSideConnection] = None
        self._process_cm: Any = None
        self._process: Any = None
        self._session_id: Optional[str] = None
        # Whether the current session_id came from the on-disk store
        # (True → `load_session` succeeded, False → we minted a fresh
        # one via `new_session`). Exposed so `pilot --session X
        # session-info` / the status socket can tell the user which
        # path fired without having to parse logs.
        self._resumed_from_store: bool = False
        self._permission_handler: Optional[PermissionHandler] = None
        # Per-session hook for canonical tool-name extraction.
        # Subclasses (e.g. `ConversationAdapterClaude`) register a
        # function here when their backend embeds the SDK tool name in
        # an out-of-band field like `_meta.claudeCode.toolName`. The
        # base transport layer treats this as opaque — all agent-
        # specific knowledge lives in the adapter that set it.
        self._tool_name_extractor: Optional[ToolNameExtractor] = None
        # Rebound by `prompt()` so a single long-lived `AcpClient` can
        # drain events into whichever turn is currently in flight.
        self._current_queue: Optional[queue.Queue[tuple[str, Any] | _Sentinel]] = None
        # Tuple of (concurrent.futures.Future, {"task": asyncio.Task})
        # set by `prompt()` and cleared by its finally block. `cancel()`
        # reads the task to force-unwind a stuck `conn.prompt`.
        self._in_flight: Optional[tuple[Any, dict[str, Any]]] = None
        self._closed = False

    def set_permission_handler(self, handler: Optional[PermissionHandler]) -> None:
        """Swap the permission handler at any point (before or during
        an active session). The client looks it up lazily per request,
        so late wiring is fine."""
        self._permission_handler = handler

    def set_tool_name_extractor(self, extractor: Optional[ToolNameExtractor]) -> None:
        """Install a canonical tool-name extractor (see `ToolNameExtractor`).
        Adapters call this from their constructor when the backend
        embeds a programmatic tool name in a non-standard field. Late
        binding is fine — `AcpClient._summarise` looks the hook up
        lazily on every event."""
        self._tool_name_extractor = extractor

    def _read_stored_session_id(self) -> Optional[str]:
        """Return the previously-saved `session_id` for this session
        slot, or None if the store path isn't configured / the file is
        missing / the payload is empty."""
        path = self.session_store_path
        if not path:
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = f.read().strip()
        except FileNotFoundError:
            return None
        except OSError as e:
            log.warning("session store read failed (%s): %s", path, e)
            return None
        return raw or None

    def _write_stored_session_id(self, session_id: str) -> None:
        """Persist `session_id` so the next launch can pick up where
        this one leaves off. Any write failure is logged and swallowed
        — a session still works without persistence, just without
        cross-launch continuity."""
        path = self.session_store_path
        if not path:
            return
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(session_id)
        except OSError as e:
            log.warning("session store write failed (%s): %s", path, e)

    def _start_loop(self) -> None:
        if self._thread is not None:
            return
        ready = threading.Event()

        def runner() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            ready.set()
            try:
                loop.run_forever()
            finally:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
                loop.close()

        self._thread = threading.Thread(
            target=runner,
            name=f"acp-loop-{os.path.basename(self.command)}",
            daemon=True,
        )
        self._thread.start()
        ready.wait()

    def _ensure_started(self) -> str:
        if self._session_id is not None:
            return self._session_id
        if self._closed:
            raise RuntimeError("ACP session already closed")
        self._start_loop()
        loop = self._loop
        assert loop is not None

        async def _bootstrap() -> str:
            client = AcpClient(
                lambda: self._current_queue,
                lambda: self._permission_handler,
                lambda: self._tool_name_extractor,
            )
            log.info(
                "acp spawn: %s %s cwd=%s mcp=%s",
                self.command,
                " ".join(self.args) if self.args else "",
                self.cwd or os.getcwd(),
                [s.name for s in self.mcp_servers],
            )
            cm = spawn_agent_process(
                client,
                self.command,
                *self.args,
                env=self.env,
                cwd=self.cwd,
                transport_kwargs={"limit": self.STREAM_LIMIT_BYTES},
                # Raw wire-log observer — enabled when the pilot module
                # logger is below DEBUG (i.e. `pilot -v toggle …`) so
                # we can diagnose protocol issues like "opencode never
                # sends session/request_permission" by inspecting the
                # actual JSON-RPC frames both directions. No-op at
                # INFO+, so normal operation pays no log-format cost.
                observers=[self._make_wire_observer()],
            )
            conn, process = await cm.__aenter__()
            self._process_cm = cm
            self._conn = conn
            self._process = process
            # Drain the agent subprocess's stderr in the background.
            # Agents routinely log over stderr (opencode prints its
            # internal permission state, claude-agent-acp surfaces
            # hook activity + SDK warnings); piping those through our
            # logger at DEBUG keeps them out of the way by default but
            # visible with `-v`, which is critical when diagnosing
            # permission-flow asymmetries between backends.
            if process.stderr is not None:
                asyncio.create_task(
                    self._drain_stderr(
                        process.stderr, os.path.basename(self.command)
                    )
                )
            caps = ClientCapabilities(
                fs=FileSystemCapabilities(read_text_file=True, write_text_file=True),
                auth=AuthCapabilities(terminal=False),
                terminal=False,
            )
            info = Implementation(name=self.client_name, version=self.client_version)
            log.debug("acp initialize: protocol=%s", PROTOCOL_VERSION)
            await conn.initialize(
                protocol_version=PROTOCOL_VERSION,
                client_capabilities=caps,
                client_info=info,
            )
            cwd = self.cwd or os.getcwd()
            mcp = list(self.mcp_servers)
            # Two-phase session handshake:
            #   1. If we have a stored session_id, try `load_session` —
            #      the agent looks it up in its own on-disk store and
            #      returns the conversation history to the client.
            #   2. On any failure (no store, agent forgot, id mismatch
            #      between providers) fall through to `new_session` and
            #      overwrite the stored id.
            # Claude's `claude-agent-acp` and `opencode acp` both
            # implement load_session; other agents may not — the try/
            # except keeps us portable.
            stored_id = self._read_stored_session_id()
            session_id: Optional[str] = None
            resumed = False
            if stored_id:
                try:
                    log.info(
                        "acp load_session attempt: id=%s store=%s",
                        stored_id,
                        self.session_store_path,
                    )
                    await conn.load_session(
                        cwd=cwd, session_id=stored_id, mcp_servers=mcp
                    )
                    session_id = stored_id
                    resumed = True
                    log.info("acp session resumed: id=%s", session_id)
                except Exception as e:
                    log.info(
                        "acp load_session failed (%s); creating a fresh session",
                        e,
                    )
            if session_id is None:
                log.debug(
                    "acp new_session: cwd=%s mcp=%s",
                    cwd,
                    [s.name for s in self.mcp_servers],
                )
                session = await conn.new_session(cwd=cwd, mcp_servers=mcp)
                session_id = session.session_id
                log.info(
                    "acp session established (fresh): id=%s store=%s",
                    session_id,
                    self.session_store_path,
                )
            self._write_stored_session_id(session_id)
            self._resumed_from_store = resumed
            return session_id

        fut = asyncio.run_coroutine_threadsafe(_bootstrap(), loop)
        self._session_id = fut.result()
        return self._session_id

    def prompt(
        self,
        user_message: str,
        event_queue: queue.Queue[tuple[str, Any] | _Sentinel],
        *,
        attachments: Optional[list["PromptAttachment"]] = None,
    ) -> None:
        """Submit a prompt and block until it resolves. `event_queue`
        becomes the session's active queue for the duration of the
        call. `attachments` is an optional list of binary / image /
        audio payloads that prefix the text block in the ACP prompt
        — callers wrap them via `pilot.image_attachment(data, mime)`
        etc.

        An `asyncio.Future` for the in-flight drive is stashed on the
        session so `cancel()` can attack from the outside."""
        session_id = self._ensure_started()
        conn = self._conn
        loop = self._loop
        assert conn is not None and loop is not None
        self._current_queue = event_queue

        drive_task_holder: dict[str, Any] = {}
        effective_message = user_message
        if self._agents_file:
            # First turn only: fence the injected instructions so the
            # agent can visually distinguish them from the user's typed
            # prose. `<SYSTEM_AGENTS>` was chosen over triple-backtick
            # because agents sometimes close stray code fences with
            # their own output — an XML-style tag reads as scoped
            # metadata in every model we ship against.
            prefix = self._agents_file
            log.info(
                "acp prompt: prefixing first turn with %d chars of AGENTS.md",
                len(prefix),
            )
            sep = "\n\n" if user_message else ""
            effective_message = (
                f"<SYSTEM_AGENTS>\n{prefix}\n</SYSTEM_AGENTS>{sep}{user_message}"
            )
            self._agents_file = ""
        blocks = self._build_prompt_blocks(effective_message, attachments or [])
        log.info(
            "acp prompt: session=%s text_len=%d attachments=%d",
            session_id,
            len(effective_message),
            len(attachments or []),
        )

        async def _drive() -> None:
            drive_task_holder["task"] = asyncio.current_task()
            try:
                await conn.prompt(prompt=blocks, session_id=session_id)
                event_queue.put(_Sentinel())
            except asyncio.CancelledError:
                event_queue.put(_Sentinel())
                raise
            except BaseException as e:
                event_queue.put(_Sentinel(error=e))
                raise

        fut = asyncio.run_coroutine_threadsafe(_drive(), loop)
        self._in_flight = (fut, drive_task_holder)
        try:
            # Block on the driver OR the subprocess; whichever wins
            # tears the turn down so the UI never gets stuck when the
            # agent crashes mid-stream.
            self._wait_for_drive_or_subprocess(fut, event_queue)
        finally:
            self._in_flight = None
            if self._current_queue is event_queue:
                self._current_queue = None

    def _wait_for_drive_or_subprocess(
        self,
        drive_future,
        event_queue: queue.Queue[tuple[str, Any] | _Sentinel],
    ) -> None:
        """Poll both the drive future and the agent subprocess. If the
        subprocess exits before the future resolves, push a sentinel
        with the exit status — otherwise a crashed agent leaves the
        generator blocked on `queue.get()` forever."""
        process = self._process
        while True:
            try:
                drive_future.result(timeout=0.5)
                return
            except FuturesTimeout:
                pass
            except BaseException:
                # Driver resolved with an exception; the sentinel is
                # already on the queue from `_drive`'s except branch.
                return
            if process is not None and process.returncode is not None:
                log.warning(
                    "ACP agent process exited rc=%s before prompt finished",
                    process.returncode,
                )
                event_queue.put(
                    _Sentinel(
                        error=RuntimeError(
                            f"acp agent exited (rc={process.returncode})"
                        )
                    )
                )
                try:
                    drive_future.cancel()
                except Exception:
                    pass
                return

    def cancel(self) -> None:
        """Cancel the in-flight prompt. Sends `session/cancel` for
        agents that honour it AND aggressively terminates our local
        asyncio task + unblocks the generator so the UI recovers even
        when the agent is unresponsive."""
        conn = self._conn
        loop = self._loop
        sid = self._session_id
        in_flight = self._in_flight
        active_queue = self._current_queue
        if conn is None or loop is None or sid is None:
            return
        log.info("acp cancel: session=%s in_flight=%s", sid, in_flight is not None)

        async def _cancel() -> None:
            try:
                await conn.cancel(session_id=sid)
            except Exception as e:
                log.warning("ACP cancel failed: %s", e)
            # Also cancel the drive task so `conn.prompt` unwinds
            # even when the agent never answers the session/cancel.
            if in_flight is not None:
                task = in_flight[1].get("task")
                if task is not None and not task.done():
                    task.cancel()

        try:
            asyncio.run_coroutine_threadsafe(_cancel(), loop)
        except RuntimeError:
            pass
        # Belt-and-braces: push a sentinel onto the queue so the
        # adapter's generator stops waiting even if the async loop
        # is itself stuck. The `_drive` task's `asyncio.CancelledError`
        # path will also push one — an extra sentinel is harmless
        # because the generator returns on the first one.
        if active_queue is not None:
            active_queue.put(_Sentinel())

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        loop = self._loop
        cm = self._process_cm
        self._conn = None
        self._session_id = None
        if loop is not None and loop.is_running():

            async def _teardown() -> None:
                if cm is not None:
                    try:
                        await cm.__aexit__(None, None, None)
                    except Exception as e:
                        log.warning("ACP subprocess teardown failed: %s", e)

            try:
                asyncio.run_coroutine_threadsafe(_teardown(), loop).result(timeout=5)
            except Exception as e:
                log.warning("ACP teardown wait failed: %s", e)
            loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=3)
        self._loop = None
        self._thread = None
        self._process_cm = None

    def reset(self) -> None:
        """Start a *fresh* ACP session without destroying the adapter.
        Sequence:

          1. Tear the current subprocess + asyncio loop down via
             `close()` (same teardown used on pilot exit).
          2. Unlink the on-disk session-id pointer so the next
             `_ensure_started` can't resume the old session.
          3. Re-arm the AGENTS.md prefix (`_agents_file`) from the
             original blob — `prompt()` consumed it at first-turn, a
             fresh session needs it re-injected.
          4. Reset the lifecycle flags so the next `prompt()` walks the
             bootstrap path again.

        The adapter stays alive and keeps its config (command, args,
        env, mcp_servers, session_store_path). The next turn spawns a
        brand-new subprocess and mints a brand-new `session_id` via
        `new_session` (never `load_session`, since the store is gone).
        """
        log.info(
            "acp reset: dropping session + rearming fresh bootstrap (store=%s)",
            self.session_store_path,
        )
        self.close()
        if self.session_store_path:
            try:
                os.unlink(self.session_store_path)
            except FileNotFoundError:
                pass
            except OSError as e:
                log.warning(
                    "session store unlink failed (%s): %s",
                    self.session_store_path,
                    e,
                )
        # Flip `_closed` back off + zero the cached session_id so
        # `_ensure_started` walks the bootstrap branch on next turn.
        self._closed = False
        self._session_id = None
        self._resumed_from_store = False
        self._agents_file = self._original_agents_file
        self._current_queue = None
        self._in_flight = None

class AcpAdapter:
    """Base class for any `lib.converse` adapter that speaks ACP.

    Subclasses set `provider` + a default `command` / `args` and can
    layer in backend-specific env munging. All kwargs flow through to
    `AcpSession` using the `kwargs.get("x") or DEFAULT` idiom that
    keeps argparse flags (None when unset) collapsing to defaults."""

    provider: Any  # Subclasses set to a `ConversationProvider` member.

    def __init__(self, **kwargs: Any):
        self._session = AcpSession(**kwargs)

    @staticmethod
    def select_option_id(
        options: list[PermissionOption], want_kind: str
    ) -> Optional[str]:
        """Pick the option_id matching `want_kind` (or its fallback
        chain). Returns None when nothing sensible is on offer so
        callers can decide whether to send `cancelled` or fall back
        to whatever the agent declared first.

        Logs at WARN when the fallback chain exhausts and we have to
        pick the first option blindly — that's the signal that
        `_KIND_FALLBACK` is missing a translation for the agent
        version we're talking to."""
        if not options:
            return None
        by_kind: dict[str, str] = {}
        for opt in options:
            if opt.kind and opt.option_id:
                by_kind.setdefault(opt.kind, opt.option_id)
        for candidate in _KIND_FALLBACK.get(want_kind, (want_kind,)):
            if candidate in by_kind:
                return by_kind[candidate]
        chosen = options[0].option_id
        log.warning(
            "select_option_id: want_kind=%s not in agent options %s; "
            "falling back to first option_id=%s",
            want_kind,
            list(by_kind.keys()),
            chosen,
        )
        return chosen

    def set_permission_handler(self, handler: Optional[PermissionHandler]) -> None:
        self._session.set_permission_handler(handler)

    def set_tool_name_extractor(self, extractor: Optional[ToolNameExtractor]) -> None:
        """Forward to the underlying session. Subclasses call this from
        their constructor (or late from higher-level wiring) when their
        backend carries the canonical tool name in an extension field —
        e.g. `ConversationAdapterClaude` pulls it from
        `_meta.claudeCode.toolName`."""
        self._session.set_tool_name_extractor(extractor)

    def reset(self) -> None:
        """Drop the current ACP session + restart fresh on the next
        turn. Keeps the adapter instance + its config alive; just
        forgets the session_id (on disk + in memory) and tears the
        subprocess down. See `AcpSession.reset` for the full teardown
        sequence."""
        self._session.reset()

    @property
    def mcp_server_names(self) -> list[str]:
        """Names of every MCP server the session handed to the agent at
        `new_session`. Exposed here so callers don't need to reach into
        `adapter._session.mcp_servers` to build palette listings."""
        return [s.name for s in self._session.mcp_servers if s.name]

    @property
    def session_id(self) -> Optional[str]:
        """Current ACP `session_id`, or None before the first turn /
        after `close()`. Surfaced for the `status` socket and the
        `session-info` CLI so users can see which conversation they
        just resumed into."""
        return self._session._session_id

    @property
    def session_resumed(self) -> bool:
        """True when the active session_id came from the on-disk store
        (`load_session` succeeded), False when we just minted it via
        `new_session`."""
        return self._session._resumed_from_store

    @property
    def session_store_path(self) -> Optional[str]:
        """Filesystem path the session_id is persisted to, or None
        when persistence is disabled."""
        return self._session.session_store_path

    def iter_events(
        self,
        user_message: str,
        *,
        attachments: Optional[list[PromptAttachment]] = None,
    ) -> Iterator[tuple[str, Any]]:
        """Yields `(kind, payload)` tuples where `kind` is one of
        `"text" | "thinking" | "tool" | "plan"`. `attachments` is
        optional — pasted images or other binary blobs flow through
        as ACP content blocks prefixed to the text prose.

        Named `iter_events` (not `turn`) so `_AcpConverseAdapter.turn`
        can define its own return type (`Iterator[TurnChunk]`) without
        the override triggering a Liskov-violation on the raw-tuple
        yield type here — this method is the low-level event stream
        the converse layer wraps, not a user-facing API."""
        event_queue: queue.Queue[tuple[str, Any] | _Sentinel] = queue.Queue()
        error_holder: dict[str, BaseException] = {}

        def driver() -> None:
            try:
                self._session.prompt(user_message, event_queue, attachments=attachments)
            except BaseException as e:  # pragma: no cover
                error_holder["error"] = e

        t = threading.Thread(target=driver, name="acp-prompt-driver", daemon=True)
        t.start()

        while True:
            item = event_queue.get()
            if isinstance(item, _Sentinel):
                if item.error is not None:
                    log.warning("ACP prompt errored: %s", item.error)
                break
            yield item

        t.join(timeout=1)
        err = error_holder.get("error")
        if err is not None and not isinstance(err, asyncio.CancelledError):
            raise err

    def cancel(self) -> None:
        self._session.cancel()

    def close(self) -> None:
        self._session.close()


# Module-level alias so existing `from lib.acp_adapter import
# select_option_id` imports in pilot.py don't need to change. The
# canonical home is `AcpAdapter.select_option_id`.
select_option_id = AcpAdapter.select_option_id
