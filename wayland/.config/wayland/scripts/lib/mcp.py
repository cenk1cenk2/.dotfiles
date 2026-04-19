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
        any plumbing around it — the callback just has to decide."""

        def handler(args: dict) -> dict:
            caller_tool = args.get("tool_name", "") or ""
            caller_input = args.get("input") or {}
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

    # -- runtime --------------------------------------------------------

    def run(self) -> None:
        """Blocking stdio JSON-RPC loop. Call from a subprocess entry
        point that claude spawns."""
        for raw in sys.stdin:
            line = raw.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            self._handle(msg)

    def _send(self, obj: dict) -> None:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()

    def _error(self, mid, code: int, message: str) -> None:
        if mid is None:
            return
        self._send({
            "jsonrpc": "2.0",
            "id": mid,
            "error": {"code": code, "message": message},
        })

    def _handle(self, msg: dict) -> None:
        method = msg.get("method")
        mid = msg.get("id")
        match method:
            case "initialize":
                self._send({
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
                })
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
                self._send({
                    "jsonrpc": "2.0",
                    "id": mid,
                    "result": {"tools": tools},
                })
            case "tools/call":
                self._dispatch_call(mid, msg.get("params") or {})
            case _:
                self._error(mid, -32601, f"method not found: {method}")

    def _dispatch_call(self, mid, params: dict) -> None:
        name = params.get("name")
        entry = self._tools.get(name)
        if entry is None:
            self._error(mid, -32602, f"unknown tool: {name!r}")

            return
        args = params.get("arguments") or {}
        try:
            raw_result = entry["handler"](args)
        except Exception as e:
            log.exception("tool handler raised")
            self._send({
                "jsonrpc": "2.0",
                "id": mid,
                "result": {
                    "content": [{"type": "text", "text": f"handler error: {e}"}],
                    "isError": True,
                },
            })

            return
        # If the handler returned the MCP content envelope directly,
        # forward verbatim. Otherwise wrap its dict into a single text
        # content block.
        if isinstance(raw_result, dict) and "content" in raw_result:
            result = raw_result
        else:
            result = {
                "content": [{"type": "text", "text": json.dumps(raw_result)}],
                "isError": False,
            }
        self._send({"jsonrpc": "2.0", "id": mid, "result": result})

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
            return False, f"overlay unavailable: {e}"
        if not raw:
            return False, "overlay closed connection"
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return False, f"overlay sent non-JSON: {raw[:120]}"

        return bool(parsed.get("approved")), str(parsed.get("reason") or "")

    return callback
