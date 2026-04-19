"""Human-friendly one-line summaries of tool-call arguments.

`PermissionRow` and the assistant-card tool-bubble detail panel both
need to turn a `(tool_name, arguments)` pair into a short preview.
Raw JSON works, but for common tool names (Bash, Read, Write, Edit,
Grep, WebFetch, Task, TodoWrite, …) we can do much better: `Bash` is
obviously `$ <command>`, `Read` is a file path + optional line range,
`Edit` is a file path, etc. The registry below keys on the exact
tool name and returns a single preview line; `format_tool_args`
dispatches through it with a JSON-pretty fallback.

MCP tools follow the `mcp__<server>__<tool>` convention — the
registry includes entries for the well-known ones in our stack
(`mcp__pilot__*`). For any other MCP tool, `_mcp_fallback` picks
the first string-ish value in the args dict so the preview at
least carries SOMETHING more useful than `{}`."""

from __future__ import annotations

import json
import os
from typing import Any, Callable

def _fmt_read(args: dict) -> str:
    path = args.get("file_path", "")
    out = f"📖 {path}"
    offset = args.get("offset")
    limit = args.get("limit")
    if offset is not None and limit is not None:
        try:
            out += f"  lines {int(offset)}..{int(offset) + int(limit)}"
        except (TypeError, ValueError):
            pass
    elif offset is not None:
        out += f"  from line {offset}"
    return out

def _fmt_write(args: dict) -> str:
    path = args.get("file_path", "")
    content = args.get("content") or ""
    return f"📝 {path}  ({len(content)} chars)"

def _fmt_edit(args: dict) -> str:
    path = args.get("file_path", "")
    replace_all = args.get("replace_all")
    suffix = "  (replace all)" if replace_all else ""
    return f"✏️  {path}{suffix}"

def _fmt_multi_edit(args: dict) -> str:
    path = args.get("file_path", "")
    edits = args.get("edits") or []
    return f"✏️  {path}  ({len(edits)} edits)"

def _fmt_grep(args: dict) -> str:
    pattern = args.get("pattern", "")
    path = args.get("path", ".")
    return f"🔍 {pattern} in {path}"

def _fmt_glob(args: dict) -> str:
    pattern = args.get("pattern", "")
    path = args.get("path")
    if path:
        return f"📂 {pattern} in {path}"
    return f"📂 {pattern}"

def _fmt_web_fetch(args: dict) -> str:
    return f"🌐 {args.get('url', '')}"

def _fmt_web_search(args: dict) -> str:
    return f"🔎 {args.get('query', '')}"

def _fmt_task(args: dict) -> str:
    subagent = args.get("subagent_type") or "agent"
    description = args.get("description") or args.get("prompt") or ""
    if len(description) > 120:
        description = description[:117] + "…"
    return f"🤖 {subagent}: {description}"

def _fmt_todo_write(args: dict) -> str:
    todos = args.get("todos") or []
    return f"📋 {len(todos)} items"

def _fmt_notebook_edit(args: dict) -> str:
    path = args.get("notebook_path") or args.get("file_path", "")
    cell_id = args.get("cell_id")
    if cell_id:
        return f"📓 {path}  cell={cell_id}"
    return f"📓 {path}"

def _fmt_kill_shell(args: dict) -> str:
    return f"☠️  shell {args.get('shell_id', '')}"

def _fmt_bash_output(args: dict) -> str:
    return f"📜 bash output {args.get('bash_id', '')}"

def _fmt_mcp_pilot_approve(args: dict) -> str:
    target = args.get("tool_name") or args.get("name") or "?"
    return f"approve({target})"

def _fmt_mcp_pilot_ask_question(args: dict) -> str:
    question = args.get("question") or ""
    if len(question) > 200:
        question = question[:197] + "…"
    return f'❓ "{question}"'

def _fmt_mcp_pilot_open(args: dict) -> str:
    url = args.get("url") or args.get("uri") or ""
    return f"↗ {url}"

def _fmt_mcp_pilot_load_skill(args: dict) -> str:
    name = args.get("name") or args.get("skill") or ""
    return f"🧠 skill: {name}"

TOOL_FORMATTERS: dict[str, Callable[[dict], str]] = {
    "Bash": lambda a: f"$ {a.get('command', '')}",
    "BashOutput": _fmt_bash_output,
    "KillShell": _fmt_kill_shell,
    "Read": _fmt_read,
    "Write": _fmt_write,
    "Edit": _fmt_edit,
    "MultiEdit": _fmt_multi_edit,
    "Grep": _fmt_grep,
    "Glob": _fmt_glob,
    "WebFetch": _fmt_web_fetch,
    "WebSearch": _fmt_web_search,
    "Task": _fmt_task,
    "TodoWrite": _fmt_todo_write,
    "NotebookEdit": _fmt_notebook_edit,
    # Well-known MCP tools in our pilot stack.
    "mcp__pilot__approve": _fmt_mcp_pilot_approve,
    "mcp__pilot__ask_question": _fmt_mcp_pilot_ask_question,
    "mcp__pilot__open": _fmt_mcp_pilot_open,
    "mcp__pilot__load_skill": _fmt_mcp_pilot_load_skill,
}

def _mcp_resource_fallback(name: str, args: dict) -> str:
    """`mcp__pilot__resource__<something>` family — we don't enumerate
    every resource tool here, so just show the trailing segment plus
    whatever name/uri the caller passed."""
    tail = name.split("__", 3)[-1] if "__" in name else name
    hint = args.get("uri") or args.get("name") or args.get("resource") or ""
    if hint:
        return f"📄 {tail}: {hint}"
    return f"📄 {tail}"

def _mcp_fallback(name: str, args: dict) -> str:
    """Last-resort formatter for an unknown `mcp__<server>__<tool>` —
    pull out the first sensible string value so the preview at least
    has something to read. Falls through to JSON if no such value
    exists. Displays the leaf tool name inline so the user still sees
    WHICH tool ran when the server prefix is long."""
    parts = name.split("__", 2)
    tail = parts[2] if len(parts) >= 3 else name
    if isinstance(args, dict):
        for key in (
            "url",
            "uri",
            "query",
            "question",
            "path",
            "name",
            "pattern",
            "text",
            "command",
        ):
            value = args.get(key)
            if isinstance(value, str) and value:
                preview = value if len(value) <= 160 else value[:157] + "…"
                return f"{tail}: {preview}"
        for value in args.values():
            if isinstance(value, str) and value:
                preview = value if len(value) <= 160 else value[:157] + "…"
                return f"{tail}: {preview}"
    try:
        return f"{tail} {json.dumps(args)}"
    except (TypeError, ValueError):
        return f"{tail} {args}"

def _coerce_args(arguments: Any) -> Any:
    """Accept the value from either `ToolCall.arguments` (raw string)
    or an already-parsed dict/list. Returns the parsed form when the
    input was a JSON string; otherwise returns the input unchanged.
    Non-JSON strings come back as-is so formatters can still fall
    through to their JSON dumps branch with something sensible."""
    if isinstance(arguments, (dict, list)):
        return arguments
    if isinstance(arguments, str):
        s = arguments.strip()
        if not s:
            return {}
        try:
            return json.loads(s)
        except (json.JSONDecodeError, ValueError):
            return arguments
    return arguments

def _fence(body: str, lang: str = "") -> str:
    """Wrap `body` in a fenced code block. Bumps the fence from three to
    four backticks when the body itself contains a triple-fence so the
    outer block doesn't close prematurely."""
    fence = "```"
    if "```" in body:
        fence = "````"
    tag = lang or ""
    return f"{fence}{tag}\n{body.rstrip()}\n{fence}"


def _md_bash(args: dict) -> str:
    cmd = args.get("command") or ""
    header = f"`bash` — {args.get('description') or ''}".rstrip(" —")
    return f"{header}\n\n{_fence(cmd, 'bash')}"


def _md_read(args: dict) -> str:
    path = args.get("file_path") or ""
    offset = args.get("offset")
    limit = args.get("limit")
    parts = [f"📖 read `{path}`"]
    if offset is not None and limit is not None:
        try:
            parts.append(f"lines {int(offset)}..{int(offset) + int(limit)}")
        except (TypeError, ValueError):
            pass
    elif offset is not None:
        parts.append(f"from line {offset}")
    return "  ".join(parts)


def _md_write(args: dict) -> str:
    path = args.get("file_path") or ""
    content = args.get("content") or ""
    body = f"📝 write `{path}` ({len(content)} chars)"
    if content:
        preview = content if len(content) <= 2000 else content[:1997] + "…"
        lang = _lang_hint_for_path(path)
        body += "\n\n" + _fence(preview, lang)
    return body


def _md_edit(args: dict) -> str:
    path = args.get("file_path") or ""
    lang = _lang_hint_for_path(path)
    replace_all = " (replace all)" if args.get("replace_all") else ""
    old = args.get("old_string") or ""
    new = args.get("new_string") or ""
    parts = [f"✏️  edit `{path}`{replace_all}"]
    if old:
        parts.append("**old:**\n" + _fence(old, lang))
    if new:
        parts.append("**new:**\n" + _fence(new, lang))
    return "\n\n".join(parts)


def _md_multi_edit(args: dict) -> str:
    path = args.get("file_path") or ""
    edits = args.get("edits") or []
    return f"✏️  multi-edit `{path}`  ({len(edits)} edits)"


def _md_grep(args: dict) -> str:
    pattern = args.get("pattern") or ""
    path = args.get("path") or "."
    glob = args.get("glob")
    parts = [f"🔍 grep `{pattern}` in `{path}`"]
    if glob:
        parts.append(f"glob=`{glob}`")
    return "  ".join(parts)


def _md_glob(args: dict) -> str:
    pattern = args.get("pattern") or ""
    path = args.get("path")
    if path:
        return f"📂 glob `{pattern}` in `{path}`"
    return f"📂 glob `{pattern}`"


def _md_web_fetch(args: dict) -> str:
    url = args.get("url") or ""
    prompt = args.get("prompt") or ""
    body = f"🌐 fetch <{url}>" if url else "🌐 fetch"
    if prompt:
        body += f"\n\n{prompt}"
    return body


def _md_web_search(args: dict) -> str:
    query = args.get("query") or ""
    return f"🔎 search `{query}`"


def _md_task(args: dict) -> str:
    subagent = args.get("subagent_type") or "agent"
    description = args.get("description") or ""
    prompt = args.get("prompt") or ""
    out = f"🤖 **{subagent}** — {description}" if description else f"🤖 **{subagent}**"
    if prompt:
        preview = prompt if len(prompt) <= 600 else prompt[:597] + "…"
        out += "\n\n" + preview
    return out


def _md_todo_write(args: dict) -> str:
    todos = args.get("todos") or []
    if not isinstance(todos, list) or not todos:
        return "📋 todos (0 items)"
    lines = [f"📋 todos ({len(todos)} items)", ""]
    for todo in todos[:20]:
        if not isinstance(todo, dict):
            continue
        status = todo.get("status") or "?"
        content = todo.get("content") or ""
        mark = {
            "completed": "✅",
            "in_progress": "🟡",
            "pending": "⚪",
        }.get(status, "·")
        lines.append(f"- {mark} {content}")
    if len(todos) > 20:
        lines.append(f"- … +{len(todos) - 20} more")
    return "\n".join(lines)


def _md_notebook_edit(args: dict) -> str:
    path = args.get("notebook_path") or args.get("file_path") or ""
    cell_id = args.get("cell_id")
    if cell_id:
        return f"📓 `{path}`  cell=`{cell_id}`"
    return f"📓 `{path}`"


def _md_mcp_pilot_ask_question(args: dict) -> str:
    question = args.get("question") or ""
    return f"❓ **question**\n\n> {question}" if question else "❓ question"


_PATH_LANG_MAP: dict[str, str] = {
    ".py": "python",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "bash",
    ".fish": "fish",
    ".js": "javascript",
    ".mjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".jsx": "jsx",
    ".go": "go",
    ".rs": "rust",
    ".rb": "ruby",
    ".php": "php",
    ".lua": "lua",
    ".java": "java",
    ".kt": "kotlin",
    ".swift": "swift",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".html": "html",
    ".css": "css",
    ".scss": "scss",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".md": "markdown",
    ".sql": "sql",
    ".dockerfile": "dockerfile",
    ".tf": "hcl",
    ".hcl": "hcl",
    ".nix": "nix",
}


def _lang_hint_for_path(path: str) -> str:
    """Map a file path's extension to a markdown-fence language hint so
    `Edit`/`Write` previews land in a syntax-highlighted block. Empty
    string (no tag) when the extension isn't recognised — the fence is
    still valid, just not coloured."""
    if not path:
        return ""
    _, ext = os.path.splitext(path.lower())
    return _PATH_LANG_MAP.get(ext, "")


TOOL_MD_FORMATTERS: dict[str, Callable[[dict], str]] = {
    "Bash": _md_bash,
    "BashOutput": lambda a: f"📜 bash output `{a.get('bash_id', '')}`",
    "KillShell": lambda a: f"☠️  shell `{a.get('shell_id', '')}`",
    "Read": _md_read,
    "Write": _md_write,
    "Edit": _md_edit,
    "MultiEdit": _md_multi_edit,
    "Grep": _md_grep,
    "Glob": _md_glob,
    "WebFetch": _md_web_fetch,
    "WebSearch": _md_web_search,
    "Task": _md_task,
    "TodoWrite": _md_todo_write,
    "NotebookEdit": _md_notebook_edit,
    "mcp__pilot__ask_question": _md_mcp_pilot_ask_question,
    "mcp__pilot__open": lambda a: f"↗ open <{a.get('url') or a.get('uri') or ''}>",
    "mcp__pilot__load_skill": lambda a: f"🧠 skill `{a.get('name') or a.get('skill') or ''}`",
    "mcp__pilot__approve": lambda a: f"approve `{a.get('tool_name') or a.get('name') or '?'}`",
}


def _md_mcp_fallback(name: str, args: Any) -> str:
    """Markdown fallback for unknown MCP tools. Emits a header with the
    leaf tool name and a JSON block with the full argument payload so
    the user sees the whole request — no silent truncation."""
    parts = name.split("__", 2)
    tail = parts[2] if len(parts) >= 3 else name
    header = f"**{tail}**"
    try:
        body = json.dumps(args, indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        body = str(args)
    return f"{header}\n\n{_fence(body, 'json')}"


def format_tool_args_md(name: str, arguments: Any) -> str:
    """Return a **markdown-rendered** summary of a tool-call invocation.

    Called from surfaces that have a full markdown pipeline (the
    permission row renders via `MarkdownMarkup`), so common tools land
    in fenced code blocks with appropriate language tags:

    - `Bash` → ```` ```bash … ``` ````
    - `Edit` / `Write` → old/new previews fenced with a lang hint
      derived from the file extension
    - `Grep` / `WebFetch` / `Task` → inline code + short prose

    For unknown MCP tools, falls back to a JSON-fenced block with the
    full argument dict so the user can read the whole payload in the
    expanded row. No character truncation happens at this layer —
    rendering is the UI's responsibility (a scrollable wrapped Label
    accepts arbitrary length)."""
    parsed = _coerce_args(arguments)

    formatter = TOOL_MD_FORMATTERS.get(name)
    if formatter is not None and isinstance(parsed, dict):
        try:
            return formatter(parsed)
        except Exception:
            pass

    if isinstance(parsed, dict) and name.startswith("mcp__"):
        try:
            return _md_mcp_fallback(name, parsed)
        except Exception:
            pass

    # Non-mcp / non-dict: either render existing string as plain, or
    # dump JSON. Wrap in a code fence so the markdown renderer treats
    # it as pre-formatted rather than trying to style punctuation.
    if isinstance(parsed, (dict, list)):
        try:
            return _fence(json.dumps(parsed, indent=2, ensure_ascii=False), "json")
        except (TypeError, ValueError):
            pass
    if isinstance(parsed, str):
        return parsed
    return str(parsed)


def format_tool_args(name: str, arguments: Any) -> str:
    """Return a short, human-readable summary line for `(name, args)`.

    Dispatches through `TOOL_FORMATTERS` on exact name match. For
    `mcp__<server>__<tool>` names that aren't registered, falls
    through to `_mcp_resource_fallback` (resource__* family) or the
    generic `_mcp_fallback`. Anything else drops to a JSON pretty-
    dump, matching PermissionRow's historical preview behaviour.

    `arguments` may be a raw JSON string (ToolCall.arguments) or an
    already-parsed dict — both work. Formatter exceptions fall
    through to the JSON branch so a mis-typed formatter can never
    crash the UI."""
    parsed = _coerce_args(arguments)

    formatter = TOOL_FORMATTERS.get(name)
    if formatter is not None and isinstance(parsed, dict):
        try:
            return formatter(parsed)
        except Exception:
            pass

    # MCP-family fallbacks. Only engage when arguments parsed to a dict;
    # we need keyed access to find a sensible preview value.
    if isinstance(parsed, dict) and name.startswith("mcp__"):
        if "__resource__" in name or name.startswith("mcp__pilot__resource__"):
            try:
                return _mcp_resource_fallback(name, parsed)
            except Exception:
                pass
        try:
            return _mcp_fallback(name, parsed)
        except Exception:
            pass

    # JSON pretty-print fallback. Mirrors the original inline code in
    # PermissionRow so behaviour on unknown tools is unchanged.
    try:
        if isinstance(parsed, (dict, list)):
            return json.dumps(parsed, indent=2)
    except (TypeError, ValueError):
        pass
    if isinstance(parsed, str):
        return parsed
    return str(parsed)
