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
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Optional

import acp
from acp import (
    Client,
    ClientSideConnection,
    PROTOCOL_VERSION,
    RequestPermissionResponse,
    spawn_agent_process,
    text_block,
)
from acp.schema import (
    AgentMessageChunk,
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
    "failed": "completed",
}

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
                    self._put(("text", text))
            elif isinstance(update, AgentThoughtChunk):
                text = _content_text(update.content)
                if text:
                    self._put(("thinking", text))
            elif isinstance(update, (ToolCallStart, ToolCallProgress)):
                self._put(("tool", _summarise_tool_call(update)))
            elif isinstance(update, UserMessageChunk):
                return
            # Plan / usage / mode updates intentionally dropped.
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
        if handler is None:
            log.warning("no permission handler set; cancelling ACP prompt")
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        summary = _summarise_tool_call(tool_call)
        loop = asyncio.get_running_loop()
        try:
            option_id = await loop.run_in_executor(None, handler, summary, options)
        except Exception as e:
            log.warning("permission handler raised: %s", e)
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        if not option_id:
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
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

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._conn: Optional[ClientSideConnection] = None
        self._process_cm: Any = None
        self._session_id: Optional[str] = None
        self._permission_handler: Optional[PermissionHandler] = None
        # Rebound by `prompt()` so a single long-lived `AcpClient` can
        # drain events into whichever turn is currently in flight.
        self._current_queue: Optional[queue.Queue[tuple[str, Any] | _Sentinel]] = None
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
            cm = spawn_agent_process(
                client,
                self.command,
                *self.args,
                env=self.env,
                cwd=self.cwd,
            )
            conn, _ = await cm.__aenter__()
            self._process_cm = cm
            self._conn = conn
            caps = ClientCapabilities(
                fs=FileSystemCapabilities(read_text_file=True, write_text_file=True),
                auth=AuthCapabilities(terminal=False),
                terminal=False,
            )
            info = Implementation(name=self.client_name, version=self.client_version)
            await conn.initialize(
                protocol_version=PROTOCOL_VERSION,
                client_capabilities=caps,
                client_info=info,
            )
            session = await conn.new_session(
                cwd=self.cwd or os.getcwd(),
                mcp_servers=list(self.mcp_servers),
            )
            return session.session_id

        fut = asyncio.run_coroutine_threadsafe(_bootstrap(), loop)
        self._session_id = fut.result()
        return self._session_id

    def prompt(
        self,
        user_message: str,
        event_queue: queue.Queue[tuple[str, Any] | _Sentinel],
    ) -> None:
        """Submit a prompt and block until it resolves. `event_queue`
        becomes the session's active queue for the duration of the call
        so the shared `AcpClient` routes streaming updates into it; a
        `_Sentinel` is pushed when the prompt completes (with `error`
        set on failure)."""
        session_id = self._ensure_started()
        conn = self._conn
        loop = self._loop
        assert conn is not None and loop is not None
        self._current_queue = event_queue

        async def _drive() -> None:
            try:
                await conn.prompt(
                    prompt=[text_block(user_message)],
                    session_id=session_id,
                )
                event_queue.put(_Sentinel())
            except BaseException as e:
                event_queue.put(_Sentinel(error=e))
                raise

        fut = asyncio.run_coroutine_threadsafe(_drive(), loop)
        try:
            fut.result()
        except BaseException:
            # Sentinel carries the error for the caller's generator to
            # observe; swallow here so cancellation is benign.
            pass
        finally:
            # Drop the slot so a late session_update from the agent
            # after the sentinel fires doesn't leak into the next turn.
            if self._current_queue is event_queue:
                self._current_queue = None

    def cancel(self) -> None:
        conn = self._conn
        loop = self._loop
        sid = self._session_id
        if conn is None or loop is None or sid is None:
            return

        async def _cancel() -> None:
            try:
                await conn.cancel(session_id=sid)
            except Exception as e:
                log.warning("ACP cancel failed: %s", e)

        try:
            asyncio.run_coroutine_threadsafe(_cancel(), loop)
        except RuntimeError:
            pass

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

    def turn(self, user_message: str) -> Iterator[tuple[str, Any]]:
        """Yields `(kind, payload)` tuples where `kind` is one of
        `"text" | "thinking" | "tool"`. `lib.converse` wraps this into
        its public `str | ThinkingChunk | ToolCall` contract; keeping
        the raw shape here means this module doesn't need to import
        from `lib.converse` (avoids a circular import)."""
        event_queue: queue.Queue[tuple[str, Any] | _Sentinel] = queue.Queue()
        error_holder: dict[str, BaseException] = {}

        def driver() -> None:
            try:
                self._session.prompt(user_message, event_queue)
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
