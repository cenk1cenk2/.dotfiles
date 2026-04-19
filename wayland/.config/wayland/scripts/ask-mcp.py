#!/usr/bin/env python3
"""MCP stdio server that routes Claude Code's permission prompts to
the ask overlay.

Claude invokes us via `--mcp-config <cfg.json> --permission-prompt-tool
mcp__ask__approve`. Each `tools/call` on our `approve` tool carries
`{tool_name, input}`; we forward over the ask overlay's Unix socket,
block until the user clicks allow/deny, and return the allow/deny
JSON shape the Claude SDK expects:

    { "behavior": "allow", "updatedInput": { … } }
    { "behavior": "deny",  "message": "…" }

The response is wrapped as MCP `content` (single text block with the
JSON stringified). See docs.anthropic.com's "user-input" section for
the contract.

Protocol: JSON-RPC 2.0 over stdio, newline-delimited. We implement
only the four methods Claude actually sends at startup + per-call:
`initialize`, `notifications/initialized`, `tools/list`, `tools/call`.
"""

import json
import os
import socket
import sys

SOCKET_PATH = os.path.join(
    os.environ.get("XDG_RUNTIME_DIR") or "/tmp",
    "wayland-ask.sock",
)
PROTOCOL_VERSION = "2025-06-18"
TOOL_NAME = "approve"

def _respond(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

def _ask_overlay(tool_name: str, tool_input):
    """Blocking round-trip to the ask overlay. Returns
    `{"approved": bool, "reason": str}`. Any transport error is
    translated to a safe `deny` so Claude doesn't race past us."""
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
            # 10-minute ceiling so a stuck UI eventually releases
            # claude instead of hanging the subprocess forever.
            s.settimeout(600)
            s.connect(SOCKET_PATH)
            s.sendall((json.dumps(payload) + "\n").encode())
            chunks = []
            while True:
                data = s.recv(4096)
                if not data:
                    break
                chunks.append(data)
                if b"\n" in data:
                    break
            raw = b"".join(chunks).decode("utf-8", errors="replace").strip()
    except (ConnectionError, FileNotFoundError, OSError, socket.timeout) as e:
        return {"approved": False, "reason": f"overlay unavailable: {e}"}
    if not raw:
        return {"approved": False, "reason": "overlay closed connection"}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"approved": False, "reason": f"overlay sent non-JSON: {raw[:120]}"}

    return {
        "approved": bool(parsed.get("approved")),
        "reason": str(parsed.get("reason") or ""),
    }

def _handle_tools_call(mid, params):
    args = params.get("arguments") or {}
    tool_name = args.get("tool_name", "")
    tool_input = args.get("input") or {}
    result = _ask_overlay(tool_name, tool_input)
    if result["approved"]:
        payload = {"behavior": "allow", "updatedInput": tool_input}
    else:
        payload = {
            "behavior": "deny",
            "message": result["reason"] or "User denied this action",
        }
    _respond({
        "jsonrpc": "2.0",
        "id": mid,
        "result": {
            "content": [{"type": "text", "text": json.dumps(payload)}],
            "isError": False,
        },
    })

def main():
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
                _respond({
                    "jsonrpc": "2.0",
                    "id": mid,
                    "result": {
                        "protocolVersion": PROTOCOL_VERSION,
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": "ask", "version": "0.1.0"},
                    },
                })
            case "notifications/initialized":
                # Notification, no response expected.
                pass
            case "tools/list":
                _respond({
                    "jsonrpc": "2.0",
                    "id": mid,
                    "result": {
                        "tools": [{
                            "name": TOOL_NAME,
                            "description": (
                                "Route a Claude tool-use request through the ask "
                                "overlay for user approval. Returns the allow/deny "
                                "JSON Claude expects from a permission-prompt tool."
                            ),
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "tool_name": {"type": "string"},
                                    "input": {"type": "object"},
                                },
                                "required": ["tool_name", "input"],
                            },
                        }],
                    },
                })
            case "tools/call":
                _handle_tools_call(mid, msg.get("params") or {})
            case _ if mid is not None:
                _respond({
                    "jsonrpc": "2.0",
                    "id": mid,
                    "error": {
                        "code": -32601,
                        "message": f"method not found: {method}",
                    },
                })

if __name__ == "__main__":
    main()
