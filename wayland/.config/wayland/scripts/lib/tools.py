"""Tool-call → markdown rendering.

Pilot's assistant-card tool bubbles and the PermissionRow overlay
both need to turn a `(tool_name, arguments)` pair into something
more useful than a raw JSON dump. This module owns that translation
via a single `ToolFormatters` class:

  - Per-tool `format_<tool>(args)` methods consume known keys and
    return markdown.
  - A `_formatters` map binds tool names (lowercased) to those
    methods. `_seed_defaults()` populates the map at construction.
  - `register(name, fn)` binds a fresh formatter; `alias(a, b)`
    makes `a` resolve to whatever `b` is bound to. Subclasses use
    both to extend the baseline for their backend.
  - Helper utilities (`_coerce_args`, `_fence`, `_pop_str`, `_pop`,
    `_truncate`, `_lang_hint_for_path`, `_leftover_json_block`) are
    staticmethods on the class — they're only useful inside
    formatters, so this keeps the namespace tight.

Per-adapter variants extend `ToolFormatters` (see
`lib.converse.OpenCodeToolFormatters`) to add opencode-only tools or
override shapes. The adapter constructs its own instance and
exposes it via `adapter.tool_formatters` so the UI can call
`adapter.tool_formatters.format(name, args)` without caring which
backend produced the call.

Two wrinkles drive dispatch:

  - **Case** — Claude's `claude-agent-acp` surfaces tool names as
    `Bash` / `Read` / `Edit`; opencode's own ACP bridge sends lower
    case (`bash` / `read` / `edit`). `format()` lowercases +
    hyphen-normalises before looking the name up.
  - **Argument shapes** — Claude uses snake_case keys (`file_path`,
    `old_string`), opencode camelCase (`filePath`, `oldString`).
    Every `format_*` method accepts both via `_pop_str` synonym lists.

MCP tools follow the `mcp__<server>__<tool>` convention. Pilot's
own MCP helpers have explicit registrations; anything else drops
into `format_mcp_fallback` which emits the leaf tool name plus a
JSON-fenced argument dump so no payload is silently swallowed."""

from __future__ import annotations

import json
import os
from typing import Any, Callable, ClassVar

Formatter = Callable[[dict], str]


class ToolFormatters:
    """Claude-shaped baseline tool-formatter registry.

    Construction runs `_seed_defaults()` to wire every `format_*`
    method in this class into the `_formatters` map under its
    canonical name, then registers a handful of aliases (opencode's
    `patch` → `edit`, Claude's `ExitPlanMode` → `plan_exit`, etc.).

    Subclasses override `_seed_defaults()` to call `super()` and then
    register their own formatters / aliases — see
    `lib.converse.OpenCodeToolFormatters`."""

    # File extension → markdown-fence language hint. Staticmethod
    # `_lang_hint_for_path` below uses this so `format_edit` can
    # syntax-highlight old/new previews by inferring the language
    # from the file path.
    _PATH_LANG_MAP: ClassVar[dict[str, str]] = {
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

    def __init__(self):
        # Tool name (lowercased, `-` → `_`) → bound formatter method.
        # Populated by `_seed_defaults()` immediately below.
        self._formatters: dict[str, Formatter] = {}
        self._seed_defaults()

    # ── Registration API ─────────────────────────────────────────────

    def _seed_defaults(self) -> None:
        """Bind every default `format_*` method under its canonical
        tool name, then wire the equivalence aliases. Subclasses
        override and call `super()._seed_defaults()` first."""
        self.register("bash", self.format_bash)
        self.register("bash_output", self.format_bash_output)
        self.register("kill_shell", self.format_kill_shell)
        self.register("read", self.format_read)
        self.register("write", self.format_write)
        self.register("edit", self.format_edit)
        self.register("multi_edit", self.format_multi_edit)
        self.register("grep", self.format_grep)
        self.register("glob", self.format_glob)
        self.register("web_fetch", self.format_web_fetch)
        self.register("web_search", self.format_web_search)
        self.register("task", self.format_task)
        self.register("todo_write", self.format_todo_write)
        self.register("notebook_edit", self.format_notebook_edit)
        self.register("plan_enter", self.format_plan_enter)
        self.register("plan_exit", self.format_plan_exit)
        # Pilot-owned MCP tools — the full `mcp__pilot__<tool>` wire
        # form is the registration key because that's what the agent
        # sends.
        self.register("mcp__pilot__ask_question", self.format_pilot_ask_question)
        self.register("mcp__pilot__open", self.format_pilot_open)
        self.register("mcp__pilot__load_skill", self.format_pilot_load_skill)

        # Casing + spelling aliases for the same underlying tool.
        self.alias("bashoutput", "bash_output")
        self.alias("killshell", "kill_shell")
        self.alias("multiedit", "multi_edit")
        self.alias("patch", "edit")  # opencode uses `patch`.
        self.alias("webfetch", "web_fetch")
        self.alias("websearch", "web_search")
        self.alias("agent", "task")
        self.alias("todowrite", "todo_write")
        self.alias("todo", "todo_write")  # opencode shortens it.
        self.alias("notebookedit", "notebook_edit")
        self.alias("enterplanmode", "plan_enter")
        self.alias("exitplanmode", "plan_exit")

    def register(self, name: str, formatter: Formatter) -> None:
        """Bind `name` to `formatter`. Case-insensitive; `-` is
        collapsed to `_`. Latest registration for a given name
        wins."""
        key = self._normalise_name(name)
        if key:
            self._formatters[key] = formatter

    def alias(self, alias: str, target: str) -> None:
        """Route `alias` to whatever formatter is registered under
        `target`. Looked up eagerly so the alias snapshots the
        currently-bound function — a later `register(target, ...)`
        does NOT retroactively change what `alias` resolves to.
        Raises `KeyError` when `target` isn't registered yet so
        typos show up at wire time, not at first tool call."""
        alias_key = self._normalise_name(alias)
        target_key = self._normalise_name(target)
        if not alias_key:
            return
        fn = self._formatters.get(target_key)
        if fn is None:
            raise KeyError(
                f"alias({alias!r} → {target!r}): target not registered "
                f"(known: {sorted(self._formatters)})"
            )
        self._formatters[alias_key] = fn

    @staticmethod
    def _normalise_name(name: str) -> str:
        """Canonical key shape: lowercase, `-` → `_`. Leading /
        trailing whitespace is stripped so `"  Bash "` lands on
        `"bash"`. Empty or None input comes back as `""` so callers
        can short-circuit."""
        if not name:
            return ""
        return name.strip().lower().replace("-", "_")

    # ── Main dispatch ───────────────────────────────────────────────

    def format(self, name: str, arguments: Any) -> str:
        """Return a markdown-rendered summary of a tool-call
        invocation. Case-insensitive dispatch through
        `_formatters`; MCP fallback for unknown `mcp__*` names;
        final JSON-fence fallback for anything else."""
        parsed = self._coerce_args(arguments)

        formatter = self._formatters.get(self._normalise_name(name))
        if formatter is not None and isinstance(parsed, dict):
            # Shallow-copy so the caller's dict isn't mutated — the
            # method pops consumed keys from the copy, and whatever
            # is left becomes the leftover JSON block.
            leftover = dict(parsed)
            try:
                body = formatter(leftover)
            except Exception:
                leftover = {}
                body = None
            if body is not None:
                return body + self._leftover_json_block(leftover)

        if isinstance(parsed, dict) and name and name.startswith("mcp__"):
            try:
                return self.format_mcp_fallback(name, parsed)
            except Exception:
                pass

        # Non-MCP / non-dict: either render existing string as plain
        # or dump JSON. Wrap in a code fence so the markdown renderer
        # treats it as pre-formatted rather than trying to style
        # punctuation.
        if isinstance(parsed, (dict, list)):
            try:
                return self._fence(
                    json.dumps(parsed, indent=2, ensure_ascii=False), "json"
                )
            except TypeError, ValueError:
                pass
        if isinstance(parsed, str):
            return parsed
        return str(parsed)

    # ── Static helpers ───────────────────────────────────────────────
    #
    # These are the primitives every formatter reaches for. Hung on
    # the class (not the module) so subclasses inherit them without
    # re-importing and so the public surface is "just ToolFormatters".

    @staticmethod
    def _coerce_args(arguments: Any) -> Any:
        """Accept the value from either `ToolCall.arguments` (raw
        string) or an already-parsed dict/list. Returns the parsed
        form when the input was a JSON string; otherwise returns the
        input unchanged. Non-JSON strings come back as-is so
        formatters can still fall through to the JSON dumps branch
        with something sensible."""
        if isinstance(arguments, (dict, list)):
            return arguments
        if isinstance(arguments, str):
            s = arguments.strip()
            if not s:
                return {}
            try:
                return json.loads(s)
            except json.JSONDecodeError, ValueError:
                return arguments
        return arguments

    @staticmethod
    def _fence(body: str, lang: str = "") -> str:
        """Wrap `body` in a fenced code block. Bumps the fence from
        three to four backticks when the body itself contains a
        triple-fence so the outer block doesn't close prematurely."""
        fence = "```"
        if "```" in body:
            fence = "````"
        tag = lang or ""
        return f"{fence}{tag}\n{body.rstrip()}\n{fence}"

    @staticmethod
    def _pop_str(args: dict, *keys: str) -> str:
        """Pop the first non-empty string value at any of `keys`
        from `args` (mutating it) and return that value. Also removes
        every other key in `keys` from `args` even when its value
        didn't qualify (empty string, wrong type) so the JSON
        leftover dump doesn't resurrect synonyms the formatter
        already decided to ignore — e.g.
        `_pop_str(args, "file_path", "filePath")` consumes BOTH so
        neither lingers in the trailing dump."""
        chosen = ""
        for key in keys:
            value = args.pop(key, None)
            if not chosen and isinstance(value, str) and value:
                chosen = value
        return chosen

    @staticmethod
    def _pop(args: dict, key: str, default: Any = None) -> Any:
        """Thin `dict.pop` wrapper, spelled out so the consume-vs-
        inspect intent is obvious at call sites that don't go through
        `_pop_str` (non-string fields: bools, lists, ints)."""
        return args.pop(key, default)

    @staticmethod
    def _truncate(text: str, limit: int) -> str:
        """Ellipsise `text` to `limit` characters when it's longer.
        The ellipsis is the single-character `…` so the limit stays
        honest."""
        if not isinstance(text, str) or len(text) <= limit:
            return text
        return text[: limit - 1] + "…"

    @classmethod
    def _lang_hint_for_path(cls, path: str) -> str:
        """Map a file path's extension to a markdown-fence language
        hint so `format_edit`/`format_write` previews land in a
        syntax-highlighted block. Empty string (no tag) when the
        extension isn't recognised — the fence is still valid, just
        not coloured."""
        if not path:
            return ""
        _, ext = os.path.splitext(path.lower())
        return cls._PATH_LANG_MAP.get(ext, "")

    @classmethod
    def _leftover_json_block(cls, leftover: dict) -> str:
        """Serialise whatever a formatter didn't pop as a trailing
        JSON code block. Empty when `leftover` is empty so well-
        specified calls emit nothing; agent-specific extras
        (opencode's `diagnostics`, `_meta` envelopes, tool-specific
        knobs the formatter doesn't know about yet) land here instead
        of being silently swallowed."""
        if not leftover:
            return ""
        try:
            body = json.dumps(leftover, indent=2, ensure_ascii=False)
        except TypeError, ValueError:
            body = str(leftover)
        return "\n\n" + cls._fence(body, "json")

    # ── Per-tool formatters ─────────────────────────────────────────
    #
    # Each method receives a mutable dict, pops the keys it consumes,
    # and returns markdown. Anything left in the dict after return is
    # dumped as a trailing JSON code block by `format()` — so
    # agent-specific extras stay visible instead of being silently
    # dropped.

    def format_bash(self, args: dict) -> str:
        command = self._pop_str(args, "command")
        description = self._pop_str(args, "description")
        run_in_background = self._pop(args, "run_in_background")
        header_bits = ["**bash**"]
        if description:
            header_bits.append(f"— {description}")
        if run_in_background:
            header_bits.append("*(background)*")
        header = " ".join(header_bits)
        if not command:
            return header
        return f"{header}\n\n{self._fence(command, 'bash')}"

    def format_bash_output(self, args: dict) -> str:
        shell_id = self._pop_str(args, "bash_id", "shell_id")
        filter_re = self._pop_str(args, "filter")
        parts = [
            f"📜 **bash output** `{shell_id}`" if shell_id else "📜 **bash output**"
        ]
        if filter_re:
            parts.append(f"filter `{filter_re}`")
        return "  ".join(parts)

    def format_kill_shell(self, args: dict) -> str:
        shell_id = self._pop_str(args, "shell_id", "bash_id")
        return (
            f"☠️  **kill shell** `{shell_id}`" if shell_id else "☠️  **kill shell**"
        )

    def format_read(self, args: dict) -> str:
        path = self._pop_str(args, "file_path", "filePath", "filepath", "path")
        offset = self._pop(args, "offset")
        limit = self._pop(args, "limit")
        parts = [f"📖 **read** `{path}`" if path else "📖 **read**"]
        if offset is not None and limit is not None:
            try:
                parts.append(f"lines {int(offset)}..{int(offset) + int(limit)}")
            except TypeError, ValueError:
                pass
        elif offset is not None:
            parts.append(f"from line {offset}")
        return "  ".join(parts)

    def format_write(self, args: dict) -> str:
        path = self._pop_str(args, "file_path", "filePath", "filepath", "path")
        content = self._pop_str(args, "content", "newString", "new_string")
        head = f"📝 **write** `{path}`" if path else "📝 **write**"
        if content:
            head += f"  *({len(content)} chars)*"
            preview = self._truncate(content, 2000)
            lang = self._lang_hint_for_path(path)
            return head + "\n\n" + self._fence(preview, lang)
        return head

    def format_edit(self, args: dict) -> str:
        """Render an Edit call for either Claude's
        `{old_string, new_string}` shape OR opencode's `{diff}`
        metadata shape. Unified output uses fenced code blocks so the
        markdown renderer handles wrapping."""
        path = self._pop_str(args, "file_path", "filePath", "filepath", "path")
        lang = self._lang_hint_for_path(path)
        # Pop both spellings so neither lingers in the leftover JSON
        # dump — Claude sends `replace_all`, opencode `replaceAll`.
        replace_flag = self._pop(args, "replace_all") or self._pop(
            args, "replaceAll"
        )
        replace_all = " *(replace all)*" if replace_flag else ""
        old = self._pop_str(args, "old_string", "oldString")
        new = self._pop_str(args, "new_string", "newString")
        diff = self._pop_str(args, "diff", "fileDiff", "filediff")

        parts = [
            f"✏️  **edit** `{path}`{replace_all}"
            if path
            else f"✏️  **edit**{replace_all}"
        ]
        if diff and not (old or new):
            parts.append("**diff:**\n" + self._fence(diff, "diff"))
        else:
            if old:
                parts.append("**old:**\n" + self._fence(old, lang))
            if new:
                parts.append("**new:**\n" + self._fence(new, lang))
        return "\n\n".join(parts)

    def format_multi_edit(self, args: dict) -> str:
        path = self._pop_str(args, "file_path", "filePath", "filepath", "path")
        # `edits` is consumed entirely — each nested record is
        # rendered in full below, so the leftover dump would be
        # redundant.
        edits = self._pop(args, "edits") or []
        count = len(edits) if isinstance(edits, list) else 0
        head = (
            f"✏️  **multi-edit** `{path}`  *({count} edits)*"
            if path
            else f"✏️  **multi-edit** *({count} edits)*"
        )
        if not isinstance(edits, list) or not edits:
            return head
        lang = self._lang_hint_for_path(path)
        blocks = [head]
        for idx, edit in enumerate(edits[:5], start=1):
            if not isinstance(edit, dict):
                continue
            old = self._pop_str(edit, "old_string", "oldString")
            new = self._pop_str(edit, "new_string", "newString")
            suffix_flag = self._pop(edit, "replace_all") or self._pop(
                edit, "replaceAll"
            )
            suffix = " *(replace all)*" if suffix_flag else ""
            piece = [f"**#{idx}**{suffix}"]
            if old:
                piece.append("_old:_ " + self._fence(self._truncate(old, 800), lang))
            if new:
                piece.append("_new:_ " + self._fence(self._truncate(new, 800), lang))
            blocks.append("\n".join(piece))
        if count > 5:
            blocks.append(f"*… +{count - 5} more edits*")
        return "\n\n".join(blocks)

    def format_grep(self, args: dict) -> str:
        pattern = self._pop_str(args, "pattern")
        path = self._pop_str(args, "path") or "."
        parts = [f"🔍 **grep** `{pattern}` in `{path}`"]
        glob = self._pop_str(args, "glob", "include")
        if glob:
            parts.append(f"glob=`{glob}`")
        file_type = self._pop_str(args, "type")
        if file_type:
            parts.append(f"type=`{file_type}`")
        output_mode = self._pop_str(args, "output_mode", "outputMode")
        if output_mode:
            parts.append(f"mode=`{output_mode}`")
        for flag_key, flag_repr in (("-i", "-i"), ("-n", "-n")):
            if self._pop(args, flag_key):
                parts.append(f"`{flag_repr}`")
        return "  ".join(parts)

    def format_glob(self, args: dict) -> str:
        pattern = self._pop_str(args, "pattern")
        path = self._pop_str(args, "path")
        if path:
            return f"📂 **glob** `{pattern}` in `{path}`"
        return f"📂 **glob** `{pattern}`"

    def format_web_fetch(self, args: dict) -> str:
        url = self._pop_str(args, "url", "uri")
        prompt = self._pop_str(args, "prompt")
        body = f"🌐 **fetch** <{url}>" if url else "🌐 **fetch**"
        if prompt:
            body += f"\n\n{self._truncate(prompt, 600)}"
        return body

    def format_web_search(self, args: dict) -> str:
        query = self._pop_str(args, "query")
        parts = [f"🔎 **search** `{query}`"] if query else ["🔎 **search**"]
        allowed = self._pop(args, "allowed_domains")
        if allowed is None:
            allowed = self._pop(args, "allowedDomains")
        else:
            self._pop(args, "allowedDomains")
        if isinstance(allowed, list) and allowed:
            parts.append(f"allowed: {', '.join(str(d) for d in allowed)}")
        blocked = self._pop(args, "blocked_domains")
        if blocked is None:
            blocked = self._pop(args, "blockedDomains")
        else:
            self._pop(args, "blockedDomains")
        if isinstance(blocked, list) and blocked:
            parts.append(f"blocked: {', '.join(str(d) for d in blocked)}")
        return "  ".join(parts)

    def format_task(self, args: dict) -> str:
        subagent = self._pop_str(args, "subagent_type", "subagentType") or "agent"
        description = self._pop_str(args, "description")
        prompt = self._pop_str(args, "prompt")
        header = (
            f"🤖 **{subagent}** — {description}"
            if description
            else f"🤖 **{subagent}**"
        )
        if prompt:
            header += "\n\n" + self._truncate(prompt, 800)
        return header

    def format_todo_write(self, args: dict) -> str:
        todos = self._pop(args, "todos") or []
        if not isinstance(todos, list) or not todos:
            return "📋 **todos** *(0 items)*"
        lines = [f"📋 **todos** *({len(todos)} items)*", ""]
        marks = {"completed": "✅", "in_progress": "🟡", "pending": "⚪"}
        for todo in todos[:20]:
            if not isinstance(todo, dict):
                continue
            status = todo.get("status") or "?"
            content = todo.get("content") or ""
            lines.append(f"- {marks.get(status, '·')} {content}")
        if len(todos) > 20:
            lines.append(f"- *… +{len(todos) - 20} more*")
        return "\n".join(lines)

    def format_notebook_edit(self, args: dict) -> str:
        path = self._pop_str(
            args,
            "notebook_path",
            "notebookPath",
            "file_path",
            "filePath",
            "filepath",
        )
        cell_id = self._pop_str(args, "cell_id", "cellId")
        head = f"📓 `{path}`" if path else "📓 notebook"
        if cell_id:
            head += f"  cell=`{cell_id}`"
        edit_mode = self._pop_str(args, "edit_mode", "editMode")
        if edit_mode:
            head += f"  mode=`{edit_mode}`"
        new_source = self._pop_str(args, "new_source", "newSource")
        if new_source:
            head += "\n\n" + self._fence(self._truncate(new_source, 1500), "python")
        return head

    def format_plan_enter(self, args: dict) -> str:
        plan = self._pop_str(args, "plan")
        head = "🗺️  **plan mode**"
        if plan:
            head += "\n\n" + self._truncate(plan, 1200)
        return head

    def format_plan_exit(self, args: dict) -> str:
        # Claude's ExitPlanMode carries the final plan prose as
        # `plan`, so inline it when present and let the wrapper
        # JSON-dump anything else the agent tacked on (a plan path,
        # agent metadata, etc.).
        plan = self._pop_str(args, "plan")
        head = "🗺️  **exit plan mode**"
        if plan:
            head += "\n\n" + self._truncate(plan, 1200)
        return head

    # ── Pilot-owned MCP helpers ─────────────────────────────────────

    def format_pilot_ask_question(self, args: dict) -> str:
        question = self._pop_str(args, "question")
        return f"❓ **question**\n\n> {question}" if question else "❓ question"

    def format_pilot_open(self, args: dict) -> str:
        url = self._pop_str(args, "url", "uri")
        return f"↗ **open** <{url}>" if url else "↗ open"

    def format_pilot_load_skill(self, args: dict) -> str:
        name = self._pop_str(args, "name", "skill")
        return f"🧠 **skill** `{name}`" if name else "🧠 skill"

    # ── MCP fallback ─────────────────────────────────────────────────

    def format_mcp_fallback(self, name: str, args: Any) -> str:
        """Markdown fallback for unknown MCP tools. Emits a header
        with the leaf tool name and a JSON block with the full
        argument payload so the user sees the whole request — no
        silent truncation.

        Unlike the per-tool formatters, this one does NOT consume the
        inline hint from `args`: the JSON dump below re-emits the
        full payload verbatim, and popping would duplicate the
        string inline AND under the code fence."""
        parts = name.split("__", 2)
        tail = parts[2] if len(parts) >= 3 else name
        header = f"**{tail}**"
        if isinstance(args, dict):
            # Pick a short inline hint if one obvious field is set —
            # keeps collapsed rows readable without needing to expand
            # the JSON.
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
                    header += f" — {self._truncate(value, 120)}"
                    break
        try:
            body = json.dumps(args, indent=2, ensure_ascii=False)
        except TypeError, ValueError:
            body = str(args)
        return f"{header}\n\n{self._fence(body, 'json')}"
