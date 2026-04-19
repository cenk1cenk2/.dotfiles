#!/usr/bin/env python3
"""Pilot's own stdio MCP server — `system` namespace.

Thin desktop-integration layer for agents: one tool today (`open`,
shelling out to `xdg-open`), structured to grow more tools as pilot
grows. Every new tool adds one entry to `_TOOLS` and one handler in
`_HANDLERS`; the generic dispatcher handles everything else.

Runs as its own subprocess under the ACP adapter's
`new_session.mcp_servers`; registered in `lib.mcp_servers` as the
catalog entry `"system"`."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any, Callable, Optional

# Ensure `lib/` (our own package) is importable when this script is
# invoked as a standalone subprocess — the spawned agent doesn't
# inherit pilot's sys.path, so we extend it here.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.skills import (  # noqa: E402
    load_references,
    load_skill_references,
    load_skills,
    parse_skill,
    read_reference,
)

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "pilot-system", "version": "1.0"}

# Pilot passes `PILOT_SKILLS_DIR` through when spawning us so the
# `resources/*` endpoints have a concrete tree to walk. Empty / unset
# → no skills exposed, just the `open` tool.
SKILLS_DIR = os.environ.get("PILOT_SKILLS_DIR", "")


def _write(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _error(req_id: Any, code: int, message: str) -> None:
    _write(
        {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}
    )


def _result(req_id: Any, result: dict) -> None:
    _write({"jsonrpc": "2.0", "id": req_id, "result": result})


def _tool_result(text: str, *, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


# ── Tools ────────────────────────────────────────────────────────────


def _tool_open(args: dict) -> dict:
    path = (args or {}).get("path")
    if not isinstance(path, str) or not path.strip():
        return _tool_result("missing `path`", is_error=True)
    target = path.strip()
    try:
        proc = subprocess.run(
            ["xdg-open", target],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return _tool_result("xdg-open not on PATH", is_error=True)
    except subprocess.TimeoutExpired:
        return _tool_result(f"xdg-open timed out opening {target!r}", is_error=True)
    if proc.returncode != 0:
        stderr = proc.stderr.strip() or f"exit {proc.returncode}"
        return _tool_result(f"xdg-open failed: {stderr}", is_error=True)
    return _tool_result(f"Opened {target}")


_TOOLS: list[dict] = [
    {
        "name": "open",
        "description": (
            "Open a URI or file path in the user's default application "
            "via xdg-open. Works for URLs (browser), absolute file "
            "paths (file manager / editor), `obsidian://`, `mailto:`, "
            "and any other scheme xdg-open recognises."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "URI or absolute file path to open.",
                },
            },
            "required": ["path"],
        },
    },
]

_HANDLERS: dict[str, Callable[[dict], dict]] = {
    "open": _tool_open,
}


# ── Resources: skills + references ───────────────────────────────────
#
# URI scheme mirrors mcphub-nvim's layout so existing skill files read
# the same way from claude-agent-acp / opencode-acp clients:
#
#   skill/<name>              → full SKILL.md body (returns --- <name> ---)
#   skill/<name>/references   → inlined content of every `references:`
#                               path declared in the skill's frontmatter
#   reference/<name>          → a single file under `references/<name>.md`


def _list_resources() -> list[dict]:
    if not SKILLS_DIR:
        return []
    out: list[dict] = []
    for skill in load_skills(SKILLS_DIR):
        out.append(
            {
                "uri": f"skill/{skill.name}",
                "name": skill.name,
                "description": skill.description,
                "mimeType": "text/markdown",
            }
        )
    for name, _path in load_references(SKILLS_DIR):
        out.append(
            {
                "uri": f"reference/{name}",
                "name": f"reference:{name}",
                "description": f"Shared reference: {name}",
                "mimeType": "text/markdown",
            }
        )
    return out


def _list_resource_templates() -> list[dict]:
    if not SKILLS_DIR:
        return []
    return [
        {
            "uriTemplate": "skill/{name}/references",
            "name": "skill-references",
            "description": "Inline every reference a skill declares in its frontmatter.",
            "mimeType": "text/markdown",
        }
    ]


def _read_resource(uri: str) -> Optional[dict]:
    if not SKILLS_DIR or not uri:
        return None
    if uri.startswith("skill/"):
        tail = uri[len("skill/"):]
        if tail.endswith("/references"):
            skill_name = tail[: -len("/references")]
            text = load_skill_references(SKILLS_DIR, skill_name)
            if text is None:
                return None
            return {"uri": uri, "mimeType": "text/markdown", "text": text}
        skill_name = tail
        skill_md = os.path.join(SKILLS_DIR, skill_name, "SKILL.md")
        skill = parse_skill(skill_md, fallback_name=skill_name)
        if skill is None:
            return None
        return {
            "uri": uri,
            "mimeType": "text/markdown",
            "text": f"--- {skill.name} ---\n{skill.body}",
        }
    if uri.startswith("reference/"):
        name = uri[len("reference/"):]
        text = read_reference(SKILLS_DIR, name)
        if text is None:
            return None
        return {"uri": uri, "mimeType": "text/markdown", "text": text}
    return None


# ── Dispatcher ───────────────────────────────────────────────────────


def _dispatch(req: dict) -> None:
    method = req.get("method")
    req_id = req.get("id")
    params = req.get("params") or {}
    if method == "initialize":
        capabilities: dict[str, Any] = {"tools": {}}
        if SKILLS_DIR:
            capabilities["resources"] = {"listChanged": False}
        _result(
            req_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": capabilities,
                "serverInfo": SERVER_INFO,
            },
        )
        return
    if method == "notifications/initialized":
        return
    if method == "tools/list":
        _result(req_id, {"tools": _TOOLS})
        return
    if method == "tools/call":
        name = params.get("name")
        handler = _HANDLERS.get(name)
        if handler is None:
            _error(req_id, -32602, f"unknown tool: {name!r}")
            return
        _result(req_id, handler(params.get("arguments") or {}))
        return
    if method == "resources/list":
        _result(req_id, {"resources": _list_resources()})
        return
    if method == "resources/templates/list":
        _result(req_id, {"resourceTemplates": _list_resource_templates()})
        return
    if method == "resources/read":
        uri = params.get("uri") or ""
        payload = _read_resource(uri)
        if payload is None:
            _error(req_id, -32602, f"unknown resource: {uri!r}")
            return
        _result(req_id, {"contents": [payload]})
        return
    if req_id is not None:
        _error(req_id, -32601, f"method not found: {method!r}")


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            _dispatch(req)
        except Exception as e:
            req_id = req.get("id") if isinstance(req, dict) else None
            if req_id is not None:
                _error(req_id, -32603, str(e))
    return 0


if __name__ == "__main__":
    sys.exit(main())
