"""Minimal MCP stdio server + `--mcp-config` builder.

Two concerns, two classes, one helper:

- `McpServer`  — stdio JSON-RPC runtime. Register tools, call `.run()`
                 inside a subprocess to serve. Callers provide handler
                 callables; we own the protocol.
- `McpConfig`  — pure builder for Claude Code's `--mcp-config` JSON.
                 Seed with initial servers at init, `.add()` more later,
                 `.write()` or `.to_dict()` to materialise.
- `socket_approval()` — callback factory for the specific case where
                 approval decisions live in another process reachable
                 over a Unix socket. `enable_approval()` + this helper
                 is the ask overlay's pattern; any other transport can
                 be plugged in by writing its own callback with the
                 `(tool_name, input) -> (approved, reason)` signature.

Design constraints the caller drove:

- Approval is togglable — don't call `enable_approval` and the server
  is a bare tool host.
- McpConfig seeds default servers (init + post-init .add), so callers
  can layer extra MCP binaries (github, filesystem, …) alongside the
  ask approval server.
- The server class doesn't know how the approval transport works. The
  callback encapsulates that — ask happens to pass a socket-backed
  one, but a web-UI caller could pass something that talks to an HTTP
  endpoint and it would work just the same.
"""

from __future__ import annotations

import json
import logging
import socket
import subprocess
import sys
from enum import StrEnum
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-06-18"

class McpCapability(StrEnum):
    """Named capabilities that `McpServer.enable()` knows how to wire
    up. Adding a new capability is a two-step change: add the enum
    member below, then add an entry to `McpServer._capability_handlers`
    that installs it (function or bound method — any callable
    accepting `(server, **kwargs)`)."""

    APPROVAL = "approval"
    QUESTION = "question"
    OPEN = "open"

# Type alias for approval callbacks. `(tool_name, input)` arrive from
# claude; the return `(approved, reason)` is translated into the
# allow/deny envelope the Claude SDK expects.
ApprovalCallback = Callable[[str, dict], "tuple[bool, str]"]

# Question callback: claude asks the user a free-form question (via
# `enable_question`), we hand it off, get back a typed answer.
QuestionCallback = Callable[[str], str]

# Route signatures for `add_approval_route`. A matcher says "I handle
# this invocation"; a handler returns the MCP allow/deny envelope
# directly (so callers keep full control over behavior/message shape).
ApprovalMatcher = Callable[[str, dict], bool]
ApprovalRouteHandler = Callable[[str, dict], dict]

DEFAULT_QUESTION_TOOL_NAMES: "tuple[str, ...]" = (
    "AskUserQuestion",
    "AskUser",
    "ask_user_question",
    "ask_user",
    "user_question",
    "UserQuestion",
    "ask_question",
    "ask",
)

def question_route(
    callback: QuestionCallback,
    tool_names: "tuple[str, ...]" = DEFAULT_QUESTION_TOOL_NAMES,
) -> "tuple[ApprovalMatcher, ApprovalRouteHandler]":
    """Convenience builder for `McpServer.add_approval_route`.

    Returns the `(matcher, handler)` pair that pivots any of the
    built-in "ask the user a question" tools (`AskUserQuestion`,
    opencode's `ask`, …) onto the typed-answer `callback(question)`.
    Comparisons are case-insensitive and ignore `-` vs `_` in the
    tool name so claude's `AskUserQuestion`, opencode's `ask-user`,
    and the generic `ask_question` all route the same way.

    The handler pulls a readable question out of common input fields
    (`question`, `prompt`, `message`, `text`, `user_message`) and
    returns a `deny`-with-`User answered: …` envelope — the only
    honest way to inject a reply through the permission-prompt
    channel in `-p` mode, where the underlying tool can't actually
    do interactive stdin. Skipped questions come back as a terse
    deny so the model doesn't loop trying to re-ask."""
    normalised = frozenset(n.lower().replace("-", "_") for n in tool_names)

    def matcher(tool_name: str, _input: dict) -> bool:
        return tool_name.lower().replace("-", "_") in normalised

    def handler(_tool_name: str, tool_input: dict) -> dict:
        question = ""
        for key in ("question", "prompt", "message", "text", "user_message"):
            v = tool_input.get(key)
            if isinstance(v, str) and v.strip():
                question = v.strip()
                break
        if not question:
            try:
                question = json.dumps(tool_input)
            except (TypeError, ValueError):
                question = str(tool_input)
        try:
            answer = callback(question)
        except Exception as e:
            log.exception("question callback raised")

            return {
                "behavior": "deny",
                "message": f"question callback error: {e}",
            }
        if answer:
            return {"behavior": "deny", "message": f"User answered: {answer}"}

        return {
            "behavior": "deny",
            "message": "User skipped the question without answering.",
        }

    return matcher, handler

class McpServer:
    """Minimal MCP JSON-RPC 2.0 stdio server.

    Register tools with `register_tool(name, description, input_schema,
    handler)` — the handler receives the raw `arguments` dict from the
    `tools/call` request and returns either (a) a dict that will be
    JSON-stringified into a single `text` content block, or (b) a dict
    already shaped as MCP content (`{"content": [...], "isError": ...}`
    detected by the presence of the `content` key)."""

    def __init__(self, name: str, version: str = "0.1.0"):
        self.name = name
        self.version = version
        self._tools: dict[str, dict] = {}
        # Registered approval routes, tried in insertion order BEFORE
        # the base approval callback. See `add_approval_route`.
        self._approval_routes: list[tuple[ApprovalMatcher, ApprovalRouteHandler]] = []
        # Dispatcher for `enable()`. Keyed by `McpCapability`, value is
        # a callable that installs the capability onto `self`. Using
        # bound methods keeps the handler signature uniform —
        # `(self, **kwargs) -> None` — and lets subclasses extend the
        # map by overriding `__init__` if they want extra capabilities.
        self._capability_handlers: dict[McpCapability, Callable[..., None]] = {
            McpCapability.APPROVAL: self._install_approval,
            McpCapability.QUESTION: self._install_question,
            McpCapability.OPEN: self._install_open,
        }

    def enable(self, capability: "str | McpCapability", **kwargs) -> None:
        """Install a capability by its enum value OR a plain string
        key registered via `register_capability`. `kwargs` are
        forwarded to the matching installer — each capability declares
        its own required and optional args. Unknown keys raise
        `KeyError` so typos at the call-site blow up early instead of
        silently enabling nothing.

        Strings and `McpCapability` members interoperate: StrEnum
        values hash as their underlying string, so `enable("approval")`
        and `enable(McpCapability.APPROVAL)` hit the same slot."""
        self._capability_handlers[capability](**kwargs)

    def register_capability(
        self,
        key: "str | McpCapability",
        handler: Callable[..., None],
    ) -> None:
        """Plug a custom installer into the capability map.

        `handler` is invoked as `handler(server, **kwargs)` whenever
        `enable(key, **kwargs)` is called, where `server` is this
        `McpServer` instance. The handler's job is to call
        `server.register_tool(...)` (and optionally
        `server.add_approval_route(...)`) — we take care of the
        dispatch glue.

        Handlers can be functions, lambdas, bound methods, or any
        callable. For class-based installers implement
        `__call__(self, server, **kwargs)` or pass a bound method.

        Keys can be `McpCapability` members or arbitrary strings for
        third-party capabilities; the two interoperate because
        `McpCapability` is a `StrEnum`."""
        self._capability_handlers[key] = lambda **kw: handler(self, **kw)

    def register_tool(
        self,
        name: str,
        description: str,
        input_schema: dict,
        handler: Callable[[dict], dict],
    ) -> None:
        self._tools[name] = {
            "name": name,
            "description": description,
            "inputSchema": input_schema,
            "handler": handler,
        }

    def _install_approval(
        self,
        callback: ApprovalCallback,
        tool_name: str = "approve",
        description: str = (
            "Ask an external approver whether a Claude tool invocation "
            "should proceed. Returns the allow/deny envelope Claude "
            "expects from a permission-prompt tool."
        ),
    ) -> None:
        """`McpCapability.APPROVAL` installer. Registers the canonical
        Claude permission-prompt tool.

        `callback(tool_name, input)` must return `(approved, reason)`.
        We handle the translation to Claude's allow/deny envelope and
        any plumbing around it — the callback just has to decide.

        Callers can layer specialized routes on top via
        `add_approval_route(matcher, handler)`; registered routes are
        tried in order before the base callback. That's how we pivot
        built-in `AskUserQuestion`-style tools over to the compose
        question banner instead of the allow/deny row, without
        baking that specific logic into the server."""

        def handler(args: dict) -> dict:
            caller_tool = args.get("tool_name", "") or ""
            caller_input = args.get("input") or {}

            for matcher, route_handler in self._approval_routes:
                try:
                    matched = matcher(caller_tool, caller_input)
                except Exception as e:
                    log.exception("approval matcher raised")

                    return {
                        "behavior": "deny",
                        "message": f"matcher error: {e}",
                    }
                if not matched:
                    continue
                try:
                    return route_handler(caller_tool, caller_input)
                except Exception as e:
                    log.exception("approval route handler raised")

                    return {
                        "behavior": "deny",
                        "message": f"route handler error: {e}",
                    }

            try:
                approved, reason = callback(caller_tool, caller_input)
            except Exception as e:
                log.exception("approval callback raised")
                return {
                    "behavior": "deny",
                    "message": f"approval callback error: {e}",
                }
            if approved:
                return {"behavior": "allow", "updatedInput": caller_input}

            return {
                "behavior": "deny",
                "message": reason or "User denied this action",
            }

        self.register_tool(
            tool_name,
            description,
            {
                "type": "object",
                "properties": {
                    "tool_name": {"type": "string"},
                    "input": {"type": "object"},
                },
                "required": ["tool_name", "input"],
            },
            handler,
        )

    def add_approval_route(
        self,
        matcher: ApprovalMatcher,
        handler: ApprovalRouteHandler,
    ) -> None:
        """Register a specialized approval route.

        When the permission tool is invoked, routes are tried in
        registration order; the first `matcher(tool_name, input)` that
        returns True delegates to its `handler(tool_name, input)`,
        which must return an MCP allow/deny envelope directly
        (e.g. `{"behavior": "allow", "updatedInput": {...}}` or
        `{"behavior": "deny", "message": "..."}`). No match falls
        through to the base callback supplied to `enable_approval`.

        Use `question_route()` for the common case of pivoting
        built-in "ask the user a question" tools onto a typed-answer
        callback; custom matchers/handlers cover anything else."""
        self._approval_routes.append((matcher, handler))

    def _install_question(
        self,
        callback: QuestionCallback,
        tool_name: str = "ask_question",
        description: str = (
            "Ask the user a free-form question and wait for them to type "
            "an answer in the overlay's compose box. Returns the answer "
            "as a string; empty answer means the user declined to answer."
        ),
    ) -> None:
        """`McpCapability.QUESTION` installer. Registers an
        `ask_question` tool claude can invoke to get a typed reply
        from the user. `callback(question)` must return the user's
        answer (string). The UI side is responsible for blocking
        until the user actually answers."""

        def handler(args: dict) -> dict:
            question = args.get("question", "") or ""
            if not question:
                return {"answer": ""}
            try:
                answer = callback(question)
            except Exception as e:
                log.exception("question callback raised")

                return {
                    "content": [{"type": "text", "text": f"callback error: {e}"}],
                    "isError": True,
                }

            return {"answer": answer or ""}

        self.register_tool(
            tool_name,
            description,
            {
                "type": "object",
                "properties": {"question": {"type": "string"}},
                "required": ["question"],
            },
            handler,
        )

    def _install_open(
        self,
        tool_name: str = "open",
        description: str = (
            "Open a URL, file path, or URI scheme via `xdg-open` — routes "
            "through the desktop's default handler for the scheme. "
            "Examples: `https://…` → browser, `obsidian://…` → Obsidian, "
            "`mailto:…` → mail client, a file path → whatever is "
            "registered for that mime type. Runs the dispatcher detached "
            "from us so the AI turn isn't blocked on the target app's "
            "lifecycle."
        ),
    ) -> None:
        """`McpCapability.OPEN` installer. Registers a generic `open`
        tool backed by `xdg-open`.

        With strict MCP mode active the permission-prompt tool fires
        FIRST (same path as any other tool), so the user gets an
        approval row before we spawn `xdg-open`. No sandboxing beyond
        that — `xdg-open` can launch anything the user's MIME / URI
        handlers point at, which is the whole point.

        Handler spawns the command detached via `Popen` + DEVNULL so
        the AI turn isn't waiting for whatever the target app does."""

        def handler(args: dict) -> dict:
            target = args.get("url") or args.get("path") or args.get("target")
            if not isinstance(target, str) or not target.strip():
                return {"opened": False, "error": "missing url / path"}
            target = target.strip()
            try:
                subprocess.Popen(
                    ["xdg-open", target],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except FileNotFoundError:
                return {"opened": False, "error": "xdg-open not on PATH"}
            except OSError as e:
                return {"opened": False, "error": f"spawn failed: {e}"}

            return {"opened": True, "target": target}

        self.register_tool(
            tool_name,
            description,
            {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": (
                            "URL, URI (https, obsidian, mailto, …), or "
                            "absolute / relative file path."
                        ),
                    },
                },
                "required": ["url"],
            },
            handler,
        )

    # -- runtime --------------------------------------------------------

    def _send(self, obj: dict) -> None:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()

    def _error(self, mid, code: int, message: str) -> None:
        if mid is None:
            return
        self._send(
            {
                "jsonrpc": "2.0",
                "id": mid,
                "error": {"code": code, "message": message},
            }
        )

    def run(self) -> None:
        """Blocking stdio JSON-RPC loop. Call from a subprocess entry
        point that claude spawns. Handles every MCP method inline —
        `initialize`, `notifications/initialized`, `tools/list`,
        `tools/call` (dispatches to the registered handler and
        wraps / forwards its result) — so there's a single place to
        trace the protocol."""
        for raw in sys.stdin:
            line = raw.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            method = msg.get("method")
            mid = msg.get("id")
            match method:
                case "initialize":
                    self._send(
                        {
                            "jsonrpc": "2.0",
                            "id": mid,
                            "result": {
                                "protocolVersion": PROTOCOL_VERSION,
                                "capabilities": {"tools": {"listChanged": False}},
                                "serverInfo": {
                                    "name": self.name,
                                    "version": self.version,
                                },
                            },
                        }
                    )
                case "notifications/initialized":
                    # Notification — no response expected.
                    pass
                case "tools/list":
                    tools = [
                        {
                            "name": t["name"],
                            "description": t["description"],
                            "inputSchema": t["inputSchema"],
                        }
                        for t in self._tools.values()
                    ]
                    self._send(
                        {
                            "jsonrpc": "2.0",
                            "id": mid,
                            "result": {"tools": tools},
                        }
                    )
                case "tools/call":
                    params = msg.get("params") or {}
                    name = params.get("name")
                    entry = self._tools.get(name)
                    if entry is None:
                        self._error(mid, -32602, f"unknown tool: {name!r}")
                        continue
                    try:
                        raw_result = entry["handler"](params.get("arguments") or {})
                    except Exception as e:
                        log.exception("tool handler raised")
                        self._send(
                            {
                                "jsonrpc": "2.0",
                                "id": mid,
                                "result": {
                                    "content": [
                                        {"type": "text", "text": f"handler error: {e}"}
                                    ],
                                    "isError": True,
                                },
                            }
                        )
                        continue
                    # If the handler returned the MCP content envelope
                    # directly, forward verbatim. Otherwise wrap its
                    # dict into a single text content block.
                    if isinstance(raw_result, dict) and "content" in raw_result:
                        result = raw_result
                    else:
                        result = {
                            "content": [
                                {"type": "text", "text": json.dumps(raw_result)}
                            ],
                            "isError": False,
                        }
                    self._send({"jsonrpc": "2.0", "id": mid, "result": result})
                case _:
                    self._error(mid, -32601, f"method not found: {method}")

class McpConfig:
    """Builder for Claude's `--mcp-config` JSON. Holds any number of
    named servers; each entry is `{command, args, env}` exactly as
    Claude's docs describe. Seed at init or extend later."""

    def __init__(self, initial_servers: Optional[dict[str, dict]] = None):
        self._servers: dict[str, dict] = {}
        for name, spec in (initial_servers or {}).items():
            # Normalise — callers can pass either a partial spec
            # (command only) or a full one; fill in empty defaults.
            self.add(
                name,
                spec.get("command", ""),
                args=spec.get("args"),
                env=spec.get("env"),
            )

    def add(
        self,
        name: str,
        command: str,
        args: Optional[list[str]] = None,
        env: Optional[dict[str, str]] = None,
    ) -> None:
        self._servers[name] = {
            "command": command,
            "args": list(args or []),
            "env": dict(env or {}),
        }

    def remove(self, name: str) -> None:
        self._servers.pop(name, None)

    def names(self) -> list[str]:
        return list(self._servers)

    def to_dict(self) -> dict[str, Any]:
        return {"mcpServers": dict(self._servers)}

    def write(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f)

def _socket_roundtrip(socket_path: str, payload: dict, timeout_s: float) -> dict:
    """Shared transport for both approval and question callbacks. Sends
    a newline-delimited JSON request over a Unix socket, reads one
    newline-terminated response, returns the parsed dict. Any transport
    error returns `{"error": "<reason>"}` so callers can degrade
    gracefully without trying to distinguish failure modes themselves."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout_s)
            s.connect(socket_path)
            s.sendall((json.dumps(payload) + "\n").encode())
            chunks: list[bytes] = []
            while True:
                data = s.recv(4096)
                if not data:
                    break
                chunks.append(data)
                if b"\n" in data:
                    break
            raw = b"".join(chunks).decode("utf-8", errors="replace").strip()
    except (ConnectionError, FileNotFoundError, OSError, socket.timeout) as e:
        return {"error": f"socket unavailable: {e}"}
    if not raw:
        return {"error": "socket closed connection"}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"error": f"non-JSON response: {raw[:120]}"}

def socket_approval(socket_path: str, timeout_s: float = 600.0) -> ApprovalCallback:
    """Return an `ApprovalCallback` that forwards every approval
    request to a Unix socket and waits for the user's verdict.

    Wire shape (newline-delimited JSON):
      request  →  {"cmd":"permission","tool_name":"…","arguments":"…"}
      response ←  {"approved": true|false, "reason": "…"}

    Any transport error degrades to a safe `deny` so claude doesn't
    race past a broken bridge."""

    def callback(tool_name: str, tool_input: dict) -> tuple[bool, str]:
        payload = {
            "cmd": "permission",
            "tool_name": tool_name,
            "arguments": (
                json.dumps(tool_input)
                if isinstance(tool_input, (dict, list))
                else str(tool_input or "")
            ),
        }
        parsed = _socket_roundtrip(socket_path, payload, timeout_s)
        if "error" in parsed:
            return False, parsed["error"]

        return bool(parsed.get("approved")), str(parsed.get("reason") or "")

    return callback

def socket_question(socket_path: str, timeout_s: float = 600.0) -> QuestionCallback:
    """Return a `QuestionCallback` that forwards the question to a Unix
    socket and waits for the user's typed answer.

    Wire shape:
      request  →  {"cmd":"question","question":"…"}
      response ←  {"answer":"…"}

    Transport errors degrade to an empty-string answer so claude can
    continue the turn with something in hand instead of blowing up."""

    def callback(question: str) -> str:
        parsed = _socket_roundtrip(
            socket_path,
            {"cmd": "question", "question": question},
            timeout_s,
        )
        if "error" in parsed:
            return ""

        return str(parsed.get("answer") or "")

    return callback
