#!/usr/bin/env -S sh -c 'exec uv run --project "$(dirname "$0")" "$0" "$@"'
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
import logging
import os
import subprocess
import sys
from typing import Any, Callable, Optional

# Ensure `lib/` (our own package) is importable when this script is
# invoked as a standalone subprocess — the spawned agent doesn't
# inherit pilot's sys.path, so we extend it here.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.cli import create_logger  # noqa: E402
from lib.skills import (  # noqa: E402
    load_references,
    load_skill_references,
    load_skills,
    parse_skill,
    read_reference,
)

# stdout is the MCP JSON-RPC channel — any byte that lands there
# outside a `_write()` call corrupts the agent's connection and tears
# the session down. `create_logger` routes through rich on stderr so
# we're safe.
create_logger(os.environ.get("PILOT_MCP_VERBOSE") == "1")
log = logging.getLogger("pilot.mcp")

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "pilot-system", "version": "1.0"}

# URI scheme for pilot-exported resources. MCP spec allows any URI
# shape but several ACP agents (opencode in particular) validate that
# `resources/list` entries have a real scheme before surfacing them to
# the LLM. `pilot://skill/<name>` and `pilot://reference/<name>` are
# what `resources/read` keys off too — keep both sides in sync.
RESOURCE_SCHEME = "pilot"

# Pilot passes `PILOT_SKILLS_DIR` through when spawning us so the
# `resources/*` endpoints have a concrete tree to walk. Empty / unset
# → no skills exposed, just the `open` tool.
SKILLS_DIR = os.environ.get("PILOT_SKILLS_DIR", "")
log.info("mcp_server starting: pid=%s SKILLS_DIR=%r", os.getpid(), SKILLS_DIR)

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
# URI scheme mirrors mcphub-nvim's layout, scoped under `pilot://` so
# strict MCP clients accept them:
#
#   pilot://skill/<name>              → full SKILL.md body
#   pilot://skill/<name>/references   → inlined content of every
#                                       `references:` path declared in
#                                       the skill's frontmatter
#   pilot://reference/<name>          → a single file under
#                                       `references/<name>.md`
#
# `_parse_resource_uri` accepts BOTH the old scheme-less form (for
# clients that cached the pre-scheme URIs) and the new `pilot://`
# form, so upgrades are transparent.

def _skill_uri(name: str) -> str:
    return f"{RESOURCE_SCHEME}://skill/{name}"

def _reference_uri(name: str) -> str:
    return f"{RESOURCE_SCHEME}://reference/{name}"

def _parse_resource_uri(uri: str) -> Optional[tuple[str, str]]:
    """Map either scheme variant into `(kind, tail)`. Returns None for
    unrecognised URIs so callers can raise a clean "unknown resource"
    error."""
    if not uri:
        return None
    prefix = f"{RESOURCE_SCHEME}://"
    body = uri[len(prefix) :] if uri.startswith(prefix) else uri
    for kind in ("skill/", "reference/"):
        if body.startswith(kind):
            return kind[:-1], body[len(kind) :]
    return None

def _list_resources() -> list[dict]:
    if not SKILLS_DIR:
        log.info("resources/list: SKILLS_DIR empty; returning nothing")
        return []
    out: list[dict] = []
    skills = load_skills(SKILLS_DIR)
    for skill in skills:
        out.append(
            {
                "uri": _skill_uri(skill.name),
                "name": skill.name,
                "description": skill.description,
                "mimeType": "text/markdown",
            }
        )
    references = load_references(SKILLS_DIR)
    for name, _path in references:
        out.append(
            {
                "uri": _reference_uri(name),
                "name": f"reference:{name}",
                "description": f"Shared reference: {name}",
                "mimeType": "text/markdown",
            }
        )
    log.info(
        "resources/list: %d skills + %d references (total=%d) from %s",
        len(skills),
        len(references),
        len(out),
        SKILLS_DIR,
    )
    return out

def _list_resource_templates() -> list[dict]:
    if not SKILLS_DIR:
        return []
    return [
        {
            "uriTemplate": f"{RESOURCE_SCHEME}://skill/{{name}}/references",
            "name": "skill-references",
            "description": "Inline every reference a skill declares in its frontmatter.",
            "mimeType": "text/markdown",
        }
    ]

def _read_resource(uri: str) -> Optional[dict]:
    if not SKILLS_DIR:
        log.info("resources/read: SKILLS_DIR empty; refusing %s", uri)
        return None
    parsed = _parse_resource_uri(uri)
    if parsed is None:
        log.warning("resources/read: unparseable uri=%r", uri)
        return None
    kind, tail = parsed
    if kind == "skill":
        if tail.endswith("/references"):
            skill_name = tail[: -len("/references")]
            text = load_skill_references(SKILLS_DIR, skill_name)
            if text is None:
                log.warning(
                    "resources/read: no skill-refs for %r under %s",
                    skill_name,
                    SKILLS_DIR,
                )
                return None
            log.info(
                "resources/read: skill-refs %r -> %d bytes",
                skill_name,
                len(text),
            )
            return {"uri": uri, "mimeType": "text/markdown", "text": text}
        skill_name = tail
        skill_md = os.path.join(SKILLS_DIR, skill_name, "SKILL.md")
        skill = parse_skill(skill_md, fallback_name=skill_name)
        if skill is None:
            log.warning("resources/read: skill %r missing at %s", skill_name, skill_md)
            return None
        body = f"--- {skill.name} ---\n{skill.body}"
        log.info("resources/read: skill %r -> %d bytes", skill_name, len(body))
        return {"uri": uri, "mimeType": "text/markdown", "text": body}
    if kind == "reference":
        name = tail
        text = read_reference(SKILLS_DIR, name)
        if text is None:
            log.warning(
                "resources/read: reference %r missing under %s/references",
                name,
                SKILLS_DIR,
            )
            return None
        log.info("resources/read: reference %r -> %d bytes", name, len(text))
        return {"uri": uri, "mimeType": "text/markdown", "text": text}
    return None

# ── Dispatcher ───────────────────────────────────────────────────────

def _dispatch(req: dict) -> None:
    method = req.get("method")
    req_id = req.get("id")
    params = req.get("params") or {}
    log.debug("rpc: method=%s id=%s", method, req_id)
    if method == "initialize":
        capabilities: dict[str, Any] = {"tools": {}}
        if SKILLS_DIR:
            capabilities["resources"] = {"listChanged": False}
        log.info(
            "initialize: capabilities=%s skills_dir=%r",
            list(capabilities.keys()),
            SKILLS_DIR,
        )
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
        log.debug("notifications/initialized")
        return
    if method == "tools/list":
        log.debug("tools/list -> %d", len(_TOOLS))
        _result(req_id, {"tools": _TOOLS})
        return
    if method == "tools/call":
        name = params.get("name")
        log.info("tools/call: name=%s", name)
        handler = _HANDLERS.get(name) if isinstance(name, str) else None
        if handler is None:
            _error(req_id, -32602, f"unknown tool: {name!r}")
            return
        _result(req_id, handler(params.get("arguments") or {}))
        return
    if method == "resources/list":
        resources = _list_resources()
        _result(req_id, {"resources": resources})
        return
    if method == "resources/templates/list":
        templates = _list_resource_templates()
        log.debug("resources/templates/list -> %d", len(templates))
        _result(req_id, {"resourceTemplates": templates})
        return
    if method == "resources/read":
        uri = params.get("uri") or ""
        payload = _read_resource(uri)
        if payload is None:
            log.warning("resources/read: unknown uri=%r", uri)
            _error(req_id, -32602, f"unknown resource: {uri!r}")
            return
        _result(req_id, {"contents": [payload]})
        return
    if req_id is not None:
        log.warning("method not found: %s", method)
        _error(req_id, -32601, f"method not found: {method!r}")

def main() -> int:
    log.info("mcp_server ready on stdin/stdout")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            log.warning("decode failed (%d bytes): %s", len(line), e)
            continue
        try:
            _dispatch(req)
        except Exception as e:
            log.exception("dispatch failed: %s", e)
            req_id = req.get("id") if isinstance(req, dict) else None
            if req_id is not None:
                _error(req_id, -32603, str(e))
    log.info("mcp_server exiting (stdin closed)")
    return 0

if __name__ == "__main__":
    sys.exit(main())
