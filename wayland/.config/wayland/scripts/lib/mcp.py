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
import sys
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-06-18"

# Type alias for approval callbacks. `(tool_name, input)` arrive from
# claude; the return `(approved, reason)` is translated into the
# allow/deny envelope the Claude SDK expects.
ApprovalCallback = Callable[[str, dict], "tuple[bool, str]"]

# Question callback: claude asks the user a free-form question (via
# `enable_question`), we hand it off, get back a typed answer.
QuestionCallback = Callable[[str], str]

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

    def enable_approval(
        self,
        callback: ApprovalCallback,
        question_callback: Optional[QuestionCallback] = None,
        tool_name: str = "approve",
        description: str = (
            "Ask an external approver whether a Claude tool invocation "
            "should proceed. Returns the allow/deny envelope Claude "
            "expects from a permission-prompt tool."
        ),
    ) -> None:
        """Register the canonical Claude permission-prompt tool.

        `callback(tool_name, input)` must return `(approved, reason)`.
        We handle the translation to Claude's allow/deny envelope and
        any plumbing around it — the callback just has to decide.

        When `question_callback` is provided AND the inbound tool is
        one of the model's built-in "ask the user a question" tools
        (`AskUserQuestion`, opencode's `ask`, etc.) we route the
        request through it instead of the approval UI. The user's
        typed answer comes back to the model as a denial message of
        the form `User answered: …` — the only honest way to inject a
        reply through the permission-prompt channel in `-p` mode,
        where the underlying tool can't actually do interactive
        stdin. Skipped questions come back as a terse deny so the
        model doesn't loop trying to re-ask."""

        def handler(args: dict) -> dict:
            caller_tool = args.get("tool_name", "") or ""
            caller_input = args.get("input") or {}

            if question_callback is not None and caller_tool.lower().replace(
                "-", "_"
            ) in frozenset(
                {
                    "askuserquestion",
                    "askuser",
                    "ask_user_question",
                    "ask_user",
                    "user_question",
                    "userquestion",
                    "ask_question",
                    "ask",
                }
            ):
                # Pull a readable question out of whatever input
                # shape the model produced. Falls back to the raw
                # input so the banner always has *something* to show.
                question = ""
                for _key in ("question", "prompt", "message", "text", "user_message"):
                    _v = caller_input.get(_key)
                    if isinstance(_v, str) and _v.strip():
                        question = _v.strip()
                        break
                if not question:
                    try:
                        question = json.dumps(caller_input)
                    except (TypeError, ValueError):
                        question = str(caller_input)
                try:
                    answer = question_callback(question)
                except Exception as e:
                    log.exception("question callback raised in approval router")

                    return {
                        "behavior": "deny",
                        "message": f"question callback error: {e}",
                    }
                if answer:
                    return {
                        "behavior": "deny",
                        "message": f"User answered: {answer}",
                    }

                return {
                    "behavior": "deny",
                    "message": "User skipped the question without answering.",
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

    def enable_question(
        self,
        callback: QuestionCallback,
        tool_name: str = "ask_question",
        description: str = (
            "Ask the user a free-form question and wait for them to type "
            "an answer in the overlay's compose box. Returns the answer "
            "as a string; empty answer means the user declined to answer."
        ),
    ) -> None:
        """Register an `ask_question` tool claude can invoke to get a
        typed reply from the user. `callback(question)` must return the
        user's answer (string). The UI side is responsible for blocking
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
