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
per-key markdown dump so no payload is silently swallowed.

## Short header vs body

Each per-tool formatter is paired with a short verb-style header
(`Execute`, `Read`, `Edit`, `Grep`, …) via `_SHORT_HEADERS`. The UI
reads the short header through `short_header(name)` and renders the
full argument detail separately — previously the bubble/card header
used `ToolCall.title` verbatim which duplicated content that also
appeared in the body below."""

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

    # Canonical (normalised) tool name → short verb-style header the
    # UI shows above the body. Kept flat instead of per-method so
    # alias lookups (`bashoutput` / `bash_output`) resolve through the
    # same table without duplicating entries on each alias. Subclasses
    # extend via `_SHORT_HEADERS_EXTRA` in `_seed_defaults`.
    _SHORT_HEADERS: ClassVar[dict[str, str]] = {
        "bash": "Execute",
        "bash_output": "Execute · output",
        "kill_shell": "Execute · kill",
        "read": "Read",
        "write": "Write",
        "edit": "Edit",
        "multi_edit": "Edit",
        "grep": "Grep",
        "glob": "Search",
        "web_fetch": "Fetch",
        "web_search": "Search",
        "task": "Task",
        "todo_write": "Todo",
        "notebook_edit": "Edit · notebook",
        "plan_enter": "Plan",
        "plan_exit": "Plan · exit",
        "mcp__pilot__ask_question": "Question",
        "mcp__pilot__open": "Open",
        "mcp__pilot__load_skill": "Skill",
    }

    # Argument-field ordering hint for the per-key fallback. Fields
    # not in this list come after, sorted alphabetically. Rough
    # priority: identifier fields first, then content fields so the
    # user sees what's being acted on before the payload.
    _FALLBACK_KEY_ORDER: ClassVar[tuple[str, ...]] = (
        "tool",
        "name",
        "id",
        "uri",
        "url",
        "path",
        "file_path",
        "filePath",
        "filepath",
        "pattern",
        "query",
        "command",
        "description",
        "summary",
        "header",
        "title",
        "question",
        "prompt",
        "create",
        "count",
        "limit",
        "offset",
        "content",
        "body",
        "text",
        "data",
    )

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
        # Propagate the short-header mapping so UI lookups through the
        # alias find the same verb. Subclass tables beat the base.
        header = self._SHORT_HEADERS.get(target_key)
        if header and alias_key not in self._SHORT_HEADERS:
            # _SHORT_HEADERS is a ClassVar; mutate the instance-owned
            # copy instead so sibling adapters don't see our aliases.
            if "_SHORT_HEADERS" not in self.__dict__:
                self._SHORT_HEADERS = dict(self._SHORT_HEADERS)
            self._SHORT_HEADERS[alias_key] = header

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

    def short_header(self, name: str) -> str:
        """Return the short verb-style header for `name` — the UI
        pairs this with the body returned by `format` so the bubble /
        card reads as `<verb>` + `<args detail>` instead of duplicating
        the same verbose string from `ToolCall.title`.

        MCP tools without an explicit registration fall back to the
        leaf tool name (`mcp__server__<leaf>`). Unknown built-ins
        stringify through a light title-casing so `"bash_output"` →
        `"Bash output"` rather than bleeding the raw identifier
        through the UI."""
        key = self._normalise_name(name)
        header = self._SHORT_HEADERS.get(key)
        if header:
            return header
        if key.startswith("mcp__"):
            parts = key.split("__", 2)
            leaf = parts[2] if len(parts) >= 3 else key
            return leaf.replace("_", " ").strip().title() or leaf
        if key:
            return key.replace("_", " ").strip().title()
        return name or "Tool"

    def format(self, name: str, arguments: Any) -> str:
        """Return a markdown-rendered summary of a tool-call
        invocation. Case-insensitive dispatch through
        `_formatters`; MCP fallback for unknown `mcp__*` names;
        final per-key markdown fallback for anything else."""
        parsed = self._coerce_args(arguments)

        formatter = self._formatters.get(self._normalise_name(name))
        if formatter is not None and isinstance(parsed, dict):
            # Shallow-copy so the caller's dict isn't mutated — the
            # method pops consumed keys from the copy, and whatever
            # is left becomes the leftover per-key block.
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

        # Non-MCP / non-dict: dict/list goes through the per-key
        # fallback so the user sees one block per field (strings
        # raw-fenced, dicts pretty-printed). Strings pass through; the
        # remaining primitives stringify.
        if isinstance(parsed, dict):
            return self._format_key_blocks(parsed)
        if isinstance(parsed, list):
            try:
                return self._fence(
                    json.dumps(parsed, indent=2, ensure_ascii=False), "json"
                )
            except (TypeError, ValueError):
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
            except (json.JSONDecodeError, ValueError):
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
    def _ordered_fallback_keys(cls, args: dict) -> list[str]:
        """Sort `args.keys()` so identifier + descriptor fields come
        first (via `_FALLBACK_KEY_ORDER`) and everything else follows
        alphabetically. Keeps the per-key fallback readable across
        different tool shapes without per-tool custom ordering."""
        priority = {key: idx for idx, key in enumerate(cls._FALLBACK_KEY_ORDER)}
        return sorted(
            args.keys(), key=lambda k: (priority.get(k, len(priority)), str(k).lower())
        )

    @classmethod
    def _format_key_blocks(cls, args: dict) -> str:
        """Render a dict as a sequence of `**key**` sections. Strings
        land in a plain fenced block (newlines preserved); dict / list
        values pretty-print through JSON; anything else stringifies.

        Replaces the old single JSON-fence dump — one monolithic JSON
        block hid the structure of large payloads (`data` strings
        spanning dozens of lines interleaved with short identifier
        fields). Per-key blocks keep both identifier and content
        visible at a glance, and let the renderer apply proper code-
        block styling to each."""
        if not args:
            return ""
        parts: list[str] = []
        for key in cls._ordered_fallback_keys(args):
            value = args[key]
            parts.append(f"**{key}**")
            parts.append(cls._render_value_block(value))
        return "\n\n".join(parts)

    @classmethod
    def _render_value_block(cls, value: Any) -> str:
        """Fence one value for the per-key fallback. Strings render
        raw so multi-line content doesn't JSON-escape into `\\n`
        sequences; dicts/lists pretty-print as JSON so structure
        stays readable."""
        if isinstance(value, str):
            return cls._fence(value)
        if isinstance(value, bool) or value is None:
            return cls._fence(str(value))
        if isinstance(value, (int, float)):
            return cls._fence(str(value))
        try:
            return cls._fence(json.dumps(value, indent=2, ensure_ascii=False), "json")
        except (TypeError, ValueError):
            return cls._fence(str(value))

    @classmethod
    def _leftover_json_block(cls, leftover: dict) -> str:
        """Render whatever a formatter didn't pop as trailing per-key
        blocks. Empty when `leftover` is empty so well-specified calls
        emit nothing; agent-specific extras (opencode's `diagnostics`,
        `_meta` envelopes, tool-specific knobs the formatter doesn't
        know about yet) land here in the same per-key shape as the
        MCP / unknown fallback."""
        if not leftover:
            return ""
        return "\n\n" + cls._format_key_blocks(leftover)

    # ── Per-tool formatters ─────────────────────────────────────────
    #
    # Each method receives a mutable dict, pops the keys it consumes,
    # and returns markdown. Anything left in the dict after return is
    # dumped as trailing per-key blocks by `format()` — so agent-
    # specific extras stay visible instead of being silently dropped.

    def format_bash(self, args: dict) -> str:
        command = self._pop_str(args, "command")
        description = self._pop_str(args, "description")
        run_in_background = self._pop(args, "run_in_background")
        bits: list[str] = []
        if description:
            bits.append(description)
        if run_in_background:
            bits.append("*(background)*")
        head = " — ".join(bits) if bits else ""
        if not command:
            return head
        suffix = self._fence(command, "bash")
        return f"{head}\n\n{suffix}" if head else suffix

    def format_bash_output(self, args: dict) -> str:
        shell_id = self._pop_str(args, "bash_id", "shell_id")
        filter_re = self._pop_str(args, "filter")
        parts: list[str] = []
        if shell_id:
            parts.append(f"`{shell_id}`")
        if filter_re:
            parts.append(f"filter `{filter_re}`")
        return "  ".join(parts)

    def format_kill_shell(self, args: dict) -> str:
        shell_id = self._pop_str(args, "shell_id", "bash_id")
        return f"`{shell_id}`" if shell_id else ""

    def format_read(self, args: dict) -> str:
        path = self._pop_str(args, "file_path", "filePath", "filepath", "path")
        offset = self._pop(args, "offset")
        limit = self._pop(args, "limit")
        parts: list[str] = []
        if path:
            parts.append(f"`{path}`")
        if offset is not None and limit is not None:
            try:
                parts.append(f"lines {int(offset)}..{int(offset) + int(limit)}")
            except (TypeError, ValueError):
                pass
        elif offset is not None:
            parts.append(f"from line {offset}")
        return "  ".join(parts)

    def format_write(self, args: dict) -> str:
        path = self._pop_str(args, "file_path", "filePath", "filepath", "path")
        content = self._pop_str(args, "content", "newString", "new_string")
        parts: list[str] = []
        if path:
            parts.append(f"`{path}`")
        if content:
            parts.append(f"*({len(content)} chars)*")
        head = "  ".join(parts)
        if content:
            preview = self._truncate(content, 2000)
            lang = self._lang_hint_for_path(path)
            fenced = self._fence(preview, lang)
            return f"{head}\n\n{fenced}" if head else fenced
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

        parts: list[str] = []
        head_bits: list[str] = []
        if path:
            head_bits.append(f"`{path}`")
        if replace_all:
            head_bits.append(replace_all.strip())
        if head_bits:
            parts.append("  ".join(head_bits))
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
        head_bits: list[str] = []
        if path:
            head_bits.append(f"`{path}`")
        head_bits.append(f"*({count} edits)*")
        head = "  ".join(head_bits)
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
        parts: list[str] = []
        if pattern:
            parts.append(f"`{pattern}` in `{path}`")
        else:
            parts.append(f"in `{path}`")
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
            return f"`{pattern}` in `{path}`" if pattern else f"in `{path}`"
        return f"`{pattern}`" if pattern else ""

    def format_web_fetch(self, args: dict) -> str:
        url = self._pop_str(args, "url", "uri")
        prompt = self._pop_str(args, "prompt")
        parts: list[str] = []
        if url:
            parts.append(f"<{url}>")
        if prompt:
            parts.append(self._truncate(prompt, 600))
        return "\n\n".join(parts)

    def format_web_search(self, args: dict) -> str:
        query = self._pop_str(args, "query")
        parts: list[str] = []
        if query:
            parts.append(f"`{query}`")
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
        header_bits = [f"**{subagent}**"]
        if description:
            header_bits.append(f"— {description}")
        header = " ".join(header_bits)
        if prompt:
            header += "\n\n" + self._truncate(prompt, 800)
        return header

    def format_todo_write(self, args: dict) -> str:
        todos = self._pop(args, "todos") or []
        if not isinstance(todos, list) or not todos:
            return "*(0 items)*"
        lines = [f"*({len(todos)} items)*", ""]
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
        parts: list[str] = []
        if path:
            parts.append(f"`{path}`")
        if cell_id:
            parts.append(f"cell=`{cell_id}`")
        edit_mode = self._pop_str(args, "edit_mode", "editMode")
        if edit_mode:
            parts.append(f"mode=`{edit_mode}`")
        head = "  ".join(parts)
        new_source = self._pop_str(args, "new_source", "newSource")
        if new_source:
            fenced = self._fence(self._truncate(new_source, 1500), "python")
            return f"{head}\n\n{fenced}" if head else fenced
        return head

    def format_plan_enter(self, args: dict) -> str:
        plan = self._pop_str(args, "plan")
        if plan:
            return self._truncate(plan, 1200)
        return ""

    def format_plan_exit(self, args: dict) -> str:
        # Claude's ExitPlanMode carries the final plan prose as
        # `plan`, so inline it when present and let the wrapper
        # JSON-dump anything else the agent tacked on (a plan path,
        # agent metadata, etc.).
        plan = self._pop_str(args, "plan")
        if plan:
            return self._truncate(plan, 1200)
        return ""

    # ── Pilot-owned MCP helpers ─────────────────────────────────────

    def format_pilot_ask_question(self, args: dict) -> str:
        question = self._pop_str(args, "question")
        return f"> {question}" if question else ""

    def format_pilot_open(self, args: dict) -> str:
        url = self._pop_str(args, "url", "uri")
        return f"<{url}>" if url else ""

    def format_pilot_load_skill(self, args: dict) -> str:
        name = self._pop_str(args, "name", "skill")
        return f"`{name}`" if name else ""

    # ── MCP fallback ─────────────────────────────────────────────────

    def format_mcp_fallback(self, name: str, args: Any) -> str:
        """Markdown fallback for unknown MCP tools. Emits the per-key
        block layout so each argument is visible inline instead of
        buried in a single JSON dump. `name` is still surfaced by the
        short-header path; here we only render the argument payload.

        When `args` isn't a dict (agent shipped a raw scalar), stringify
        it through the same value renderer so the output shape stays
        consistent."""
        if isinstance(args, dict):
            return self._format_key_blocks(args)
        return self._render_value_block(args)
