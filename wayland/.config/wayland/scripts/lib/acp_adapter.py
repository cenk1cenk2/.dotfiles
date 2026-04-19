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
from typing import Any, Callable, Iterator, Optional

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

# Raises asyncio's default 64KB line-buffer ceiling. ACP agents emit
# newline-delimited JSON-RPC frames that routinely cross the default
# on turns that stream a tool with a large `raw_input` / `raw_output`
# (file reads, image blocks already base64-encoded, long diff patches),
# and the SDK surfaces that as `LimitOverrunError` inside the receive
# loop — which tears the connection down and leaves the UI stuck on a
# half-delivered reply. Anthropic's streaming payloads stay well under
# 16MB per line, so bumping the ceiling to that buys headroom without
# making us buffer anything we wouldn't have already. If an agent ever
# ships a single frame larger than this, the right fix is on the agent
# side — we'd rather crash here than silently wedge."""
_ACP_STREAM_LIMIT_BYTES = 16 * 1024 * 1024

# Preference order per PermissionRow action. When the agent didn't ship
# the exact kind we'd like (opencode offers only `allow_once / allow_always
# / reject_once`, no `reject_always`), fall through to the closest match.
_KIND_FALLBACK: dict[str, tuple[str, ...]] = {
    "allow_once": ("allow_once", "allow_always"),
    "allow_always": ("allow_always", "allow_once"),
    "reject_once": ("reject_once", "reject_always"),
    "reject_always": ("reject_always", "reject_once"),
}

@dataclass(frozen=True)
class ToolCallSummary:
    """Lightweight snapshot of an ACP ToolCallUpdate shaped for the
    permission-handler callback."""

    tool_id: str
    name: str
    arguments: str
    status: str


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


def image_attachment(data: bytes, mime_type: str = "image/png") -> PromptAttachment:
    return PromptAttachment(mime_type=mime_type, data=data)


@dataclass(frozen=True)
class PlanItem:
    """One entry in an `AgentPlanUpdate`. Agents emit a fresh plan
    list on every `notify::plan` — we re-render the whole thing each
    time rather than diffing."""

    content: str
    status: str  # "pending" | "in_progress" | "completed"
    priority: str  # "low" | "medium" | "high"

PermissionHandler = Callable[[ToolCallSummary, list[PermissionOption]], Optional[str]]
"""Invoked from the ACP worker thread when the agent asks for
permission. MUST block until the user decides and return the
`option_id` to send back — or None to cancel the prompt."""

def select_option_id(options: list[PermissionOption], want_kind: str) -> Optional[str]:
    """Pick the option_id matching `want_kind` (or its fallback chain).
    Returns None when nothing sensible is on offer so callers can decide
    whether to send `cancelled` or fall back to whatever the agent
    declared first."""
    if not options:
        return None
    by_kind: dict[str, str] = {}
    for opt in options:
        if opt.kind and opt.option_id:
            by_kind.setdefault(opt.kind, opt.option_id)
    for candidate in _KIND_FALLBACK.get(want_kind, (want_kind,)):
        if candidate in by_kind:
            return by_kind[candidate]
    return options[0].option_id

def _content_text(content: Any) -> Optional[str]:
    """ACP content blocks are a discriminated union; we only surface
    the text variant (chat + thought chunks)."""
    if isinstance(content, TextContentBlock):
        return content.text or None
    text = getattr(content, "text", None)
    if isinstance(text, str):
        return text or None
    return None

AcpMcpServer = HttpMcpServer | SseMcpServer | McpServerStdio

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

_ACP_STATUS_MAP: dict[str, str] = {
    "pending": "pending",
    "in_progress": "running",
    "completed": "completed",
    "failed": "failed",
}

def _build_prompt_blocks(
    user_message: str,
    attachments: list[PromptAttachment],
) -> list:
    """Compose the ACP prompt payload. Attachments prefix the text so
    their content is visible BEFORE the prose (matches how agents
    typically quote images in responses). A text block always comes
    out last — even an empty prose turn keeps the content array
    non-empty which the ACP spec requires."""
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
            # No inline bytes; reference the URI directly. `uri` alone
            # on an image_block is the "server-resolved" shape from
            # MCP `resources/read` results.
            blocks.append(
                image_block(data="", mime_type=att.mime_type, uri=att.uri)
            )
    blocks.append(text_block(user_message))
    return blocks


def _summarise_tool_call(
    update: ToolCallUpdate | ToolCallStart | ToolCallProgress,
) -> ToolCallSummary:
    tool_id = update.tool_call_id or ""
    name = update.title or update.kind or "tool"
    raw = update.raw_input
    if isinstance(raw, (dict, list)):
        try:
            args = json.dumps(raw)
        except (TypeError, ValueError):
            args = str(raw)
    elif raw is None:
        args = ""
    else:
        args = str(raw)
    return ToolCallSummary(
        tool_id=tool_id,
        name=str(name),
        arguments=args,
        status=_ACP_STATUS_MAP.get(update.status or "", "pending"),
    )

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
    ):
        self._queue_lookup = queue_lookup
        self._permission_handler_lookup = permission_handler_lookup

    def _put(self, item: tuple[str, Any] | _Sentinel) -> None:
        q = self._queue_lookup()
        if q is not None:
            q.put(item)

    async def session_update(self, session_id: str, update: Any, **_: Any) -> None:
        try:
            if isinstance(update, AgentMessageChunk):
                text = _content_text(update.content)
                if text:
                    log.debug("acp update: text chunk len=%d", len(text))
                    self._put(("text", text))
            elif isinstance(update, AgentThoughtChunk):
                text = _content_text(update.content)
                if text:
                    log.debug("acp update: thinking chunk len=%d", len(text))
                    self._put(("thinking", text))
            elif isinstance(update, (ToolCallStart, ToolCallProgress)):
                summary = _summarise_tool_call(update)
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
                log.debug(
                    "acp update: dropped kind=%s", type(update).__name__
                )
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
        summary = _summarise_tool_call(tool_call)
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
            raise acp.RequestError.internal_error(str(e))
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
            raise acp.RequestError.internal_error(str(e))
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
        self._agents_file: str = (raw_prefix or "").strip()

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._conn: Optional[ClientSideConnection] = None
        self._process_cm: Any = None
        self._process: Any = None
        self._session_id: Optional[str] = None
        self._permission_handler: Optional[PermissionHandler] = None
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
                transport_kwargs={"limit": _ACP_STREAM_LIMIT_BYTES},
            )
            conn, process = await cm.__aenter__()
            self._process_cm = cm
            self._conn = conn
            self._process = process
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
            log.debug(
                "acp new_session: cwd=%s mcp=%s",
                self.cwd or os.getcwd(),
                [s.name for s in self.mcp_servers],
            )
            session = await conn.new_session(
                cwd=self.cwd or os.getcwd(),
                mcp_servers=list(self.mcp_servers),
            )
            log.info("acp session established: id=%s", session.session_id)
            return session.session_id

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
        blocks = _build_prompt_blocks(effective_message, attachments or [])
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
                    _Sentinel(error=RuntimeError(
                        f"acp agent exited (rc={process.returncode})"
                    ))
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

class AcpAdapter:
    """Base class for any `lib.converse` adapter that speaks ACP.

    Subclasses set `provider` + a default `command` / `args` and can
    layer in backend-specific env munging. All kwargs flow through to
    `AcpSession` using the `kwargs.get("x") or DEFAULT` idiom that
    keeps argparse flags (None when unset) collapsing to defaults."""

    provider: Any  # Subclasses set to a `ConversationProvider` member.

    def __init__(self, **kwargs: Any):
        self._session = AcpSession(**kwargs)

    def set_permission_handler(self, handler: Optional[PermissionHandler]) -> None:
        self._session.set_permission_handler(handler)

    def turn(
        self,
        user_message: str,
        *,
        attachments: Optional[list[PromptAttachment]] = None,
    ) -> Iterator[tuple[str, Any]]:
        """Yields `(kind, payload)` tuples where `kind` is one of
        `"text" | "thinking" | "tool" | "plan"`. `attachments` is
        optional — pasted images or other binary blobs flow through
        as ACP content blocks prefixed to the text prose."""
        event_queue: queue.Queue[tuple[str, Any] | _Sentinel] = queue.Queue()
        error_holder: dict[str, BaseException] = {}

        def driver() -> None:
            try:
                self._session.prompt(
                    user_message, event_queue, attachments=attachments
                )
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
