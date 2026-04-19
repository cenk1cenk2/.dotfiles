"""Pango markup renderers for assistant content.

Two related utilities live here because they feed the same surface —
`Gtk.Label.set_markup()` — and both need to emit XML-flavoured Pango
span strings that concatenate cleanly with the other:

  - `highlight_code(source, language, background=None)` — syntax-
    highlight a code block via pygments, emit one `<span foreground=…>`
    per token, optionally wrap the whole thing in a single outer
    `<span background=…>` so the block reads as one cohesive surface
    rather than per-segment highlights.

  - `MarkdownMarkup().render(text)` — walk a CommonMark token stream
    and emit Pango markup for headings / bold / italic / inline code /
    fenced blocks (delegated to `highlight_code`) / links / lists /
    blockquotes / rules. Output is safe to feed straight into
    `Gtk.Label.set_markup(...)`.

The module is intentionally gi-free — it only uses `html.escape` for
XML escaping, so importing it from a headless subprocess (e.g. the MCP
server branch) doesn't drag Gtk in."""

from __future__ import annotations

import logging
import re
from html import escape as _xml_escape
from io import StringIO
from typing import Optional

from markdown_it import MarkdownIt
from pygments import highlight  # type: ignore[import-not-found]
from pygments.formatter import Formatter
from pygments.lexers import (  # type: ignore[import-not-found]
    TextLexer,
    get_lexer_by_name,
    guess_lexer,
)
from pygments.token import Token
from pygments.util import ClassNotFound  # type: ignore[import-not-found]

log = logging.getLogger(__name__)

# ── Syntax highlighting ─────────────────────────────────────────────

# One Dark palette — matches the overlay's onedarker pilot.css tokens so
# fenced code reads as a first-class citizen of the sidebar rather than
# an alien TextMate embed. Keys are pygments token types, walked parent-
# upward to find a match (e.g. `Token.Name.Function.Magic` falls through
# to `Token.Name.Function`, then `Token.Name`, then the `_DEFAULT_FG`).
_COLORS = {
    Token.Keyword: "#c678dd",
    Token.Keyword.Constant: "#d19a66",
    Token.Keyword.Declaration: "#c678dd",
    Token.Keyword.Namespace: "#c678dd",
    Token.Keyword.Pseudo: "#c678dd",
    Token.Keyword.Reserved: "#c678dd",
    Token.Keyword.Type: "#e5c07b",
    Token.Name.Builtin: "#e5c07b",
    Token.Name.Builtin.Pseudo: "#e5c07b",
    Token.Name.Function: "#61afef",
    Token.Name.Function.Magic: "#61afef",
    Token.Name.Class: "#e5c07b",
    Token.Name.Decorator: "#61afef",
    Token.Name.Exception: "#e5c07b",
    Token.Name.Namespace: "#e5c07b",
    Token.Name.Attribute: "#d19a66",
    Token.Name.Tag: "#e06c75",
    Token.Name.Variable: "#e06c75",
    Token.Name.Constant: "#d19a66",
    Token.String: "#98c379",
    Token.String.Doc: "#5c6370",
    Token.String.Escape: "#56b6c2",
    Token.String.Interpol: "#56b6c2",
    Token.Literal: "#d19a66",
    Token.Number: "#d19a66",
    Token.Comment: "#5c6370",
    Token.Comment.Preproc: "#c678dd",
    Token.Operator: "#56b6c2",
    Token.Operator.Word: "#c678dd",
    Token.Punctuation: "#abb2bf",
    Token.Generic.Deleted: "#e06c75",
    Token.Generic.Inserted: "#98c379",
    Token.Generic.Heading: "#61afef",
    Token.Generic.Subheading: "#61afef",
    Token.Generic.Emph: "#c678dd",
    Token.Generic.Strong: "#e5c07b",
    Token.Name: "#abb2bf",
    Token.Text: "#abb2bf",
}

_DEFAULT_FG = "#abb2bf"

def _token_color(ttype) -> str:
    """Walk the pygments TokenType chain up toward `Token` until we find
    a colour mapping. Returns the fallback foreground when nothing in
    the chain is registered — pygments' type tree is comprehensive so
    this is rare for mainstream languages."""
    while ttype not in _COLORS and getattr(ttype, "parent", None) is not None:
        ttype = ttype.parent
    return _COLORS.get(ttype, _DEFAULT_FG)

class PangoFormatter(Formatter):
    """Pygments Formatter that builds a single flat string of `<span
    foreground="#…">token</span>` runs. `highlight_code` wraps the whole
    result in ONE outer span that carries the block background so the
    rendered code block has a single cohesive background rather than
    per-segment highlights."""

    name = "Pango"
    aliases = ["pango"]
    filenames: list[str] = []

    def format(self, tokensource, outfile):  # type: ignore[override]
        for ttype, value in tokensource:
            if not value:
                continue
            colour = _token_color(ttype)
            outfile.write(f'<span foreground="{colour}">{_xml_escape(value)}</span>')

def highlight_code(
    source: str,
    language: Optional[str],
    background: Optional[str] = None,
) -> str:
    """Return a Pango-markup block for `source` tagged with `language`.

    When `background` is supplied, the whole highlighted output is
    wrapped in a single outer `<span background="…">` so the code block
    reads as one continuous surface rather than per-token segments.
    Falls back to plain escaped text when the language is unknown or
    pygments chokes. Output is safe to splice into a larger Pango
    markup document — every text run is escaped before emission."""

    lexer = None
    if language:
        try:
            lexer = get_lexer_by_name(language, stripnl=False)
        except ClassNotFound:
            lexer = None
    if lexer is None:
        try:
            lexer = guess_lexer(source)
        except ClassNotFound:
            lexer = TextLexer(stripnl=False)

    buf = StringIO()
    try:
        highlight(source, lexer, PangoFormatter(), buf)
        body = buf.getvalue()
    except Exception:
        # Pygments occasionally raises on malformed input (e.g. partial
        # streams mid-token). Degrade to plain escaped text so the
        # caller's markup stays valid.
        body = _xml_escape(source)

    if background:
        return f'<span background="{_xml_escape(background)}">{body}</span>'
    return body

# ── CommonMark → Pango markup ───────────────────────────────────────

class MarkdownMarkup:
    """Render CommonMark to a Pango markup string for `Gtk.Label.set_markup`.

    Chosen over a TextView+TextBuffer+TextTags pipeline because:

    * `Gtk.Label` lays out synchronously during `measure()`, so a freshly
      populated card reports its true natural height on the first layout
      pass. TextView defers validation and returns a single-line natural
      height until the buffer is walked post-realize, which is what made
      user cards render collapsed until the assistant reply forced a
      re-layout.
    * `<a href="…">` inside Pango markup drives the Label's native
      `activate-link` signal, so link clicks don't need buffer-coordinate
      math or a custom GestureClick.
    * Streaming stays cheap: each chunk we re-parse + re-emit markup and
      call `label.set_markup()`. Markdown-it runs in microseconds at the
      sizes AI responses produce."""

    HEADING_SIZES = {1: "x-large", 2: "large", 3: "medium"}
    LINK_COLOR = "#61afef"
    CODE_BG = "#17191e"
    INLINE_CODE_BG = "#2c333d"
    FG_COLOR = "#abb2bf"

    def __init__(self):
        # CommonMark baseline plus GFM `table` + `strikethrough` rules
        # — agents emit these regularly. Skip `linkify` (needs an
        # optional dep) and `gfm-like` preset (pulls linkify in).
        self._md = MarkdownIt("commonmark").enable(["table", "strikethrough"])

    def render(self, text: str) -> str:
        """Parse `text` as CommonMark+GFM and return Pango markup. Safe
        to feed straight to `Gtk.Label.set_markup()`."""
        tokens = self._md.parse(text)
        out: list[str] = []
        self._walk(tokens, out, list_stack=[], table_state=[])
        return "".join(out).rstrip(" \n\t")

    @staticmethod
    def _esc(text: str) -> str:
        # Pango markup is XML-flavoured; escape every `&`, `<`, `>`,
        # `"`, `'`. `html.escape(s, quote=True)` matches what GLib's
        # `markup_escape_text` does, without pulling gi in.
        return _xml_escape(text, quote=True)

    def _walk(self, tokens, out, list_stack, table_state) -> None:
        for tok in tokens:
            self._handle(tok, out, list_stack, table_state)

    def _handle(self, tok, out, list_stack, table_state) -> None:  # noqa: C901
        match tok.type:
            case "heading_open":
                level = int(tok.tag[1])
                size = self.HEADING_SIZES.get(level, "medium")
                out.append(f'<span size="{size}" weight="bold">')
            case "heading_close":
                out.append("</span>\n\n")
            case "paragraph_open":
                pass
            case "paragraph_close":
                # Tight lists use paragraphs for item bodies; emit one
                # break instead of two so list items don't double-space.
                out.append("\n" if list_stack else "\n\n")
            case "inline":
                self._walk(tok.children or [], out, list_stack, table_state)
            case "text":
                out.append(self._esc(tok.content))
            case "softbreak":
                out.append(" ")
            case "hardbreak":
                out.append("\n")
            case "strong_open":
                out.append("<b>")
            case "strong_close":
                out.append("</b>")
            case "em_open":
                out.append("<i>")
            case "em_close":
                out.append("</i>")
            case "code_inline":
                out.append(
                    f'<span font_family="monospace" '
                    f'background="{self.INLINE_CODE_BG}">'
                    f"{self._esc(tok.content)}</span>"
                )
            case "link_open":
                href = self._esc(tok.attrGet("href") or "")
                out.append(
                    f'<a href="{href}">'
                    f'<span foreground="{self.LINK_COLOR}" underline="single">'
                )
            case "link_close":
                out.append("</span></a>")
            case "fence" | "code_block":
                content = tok.content.rstrip("\n")
                # `tok.info` on a fence is the language string after the
                # opening triple-backticks (e.g. ```python foo → "python
                # foo"). We take the first whitespace-delimited word and
                # let `highlight_code` pygments-dispatch from there.
                # `code_block` (indented) has no language hint at all —
                # None triggers pygments' `guess_lexer` fallback.
                info = (getattr(tok, "info", "") or "").strip().split()
                language = info[0] if info else None
                highlighted = highlight_code(content, language, background=self.CODE_BG)
                out.append(f'<span font_family="monospace">{highlighted}</span>\n\n')
            case "bullet_list_open":
                list_stack.append(["bullet", 0])
            case "ordered_list_open":
                list_stack.append(["ordered", int(tok.attrGet("start") or 1)])
            case "bullet_list_close" | "ordered_list_close":
                list_stack.pop()
                if not list_stack:
                    out.append("\n")
            case "list_item_open":
                ctx = list_stack[-1] if list_stack else None
                indent = "  " * max(0, len(list_stack) - 1)
                if ctx and ctx[0] == "ordered":
                    out.append(f"{indent}{ctx[1]}. ")
                    ctx[1] += 1
                else:
                    out.append(f"{indent}• ")
            case "list_item_close":
                pass
            case "blockquote_open":
                out.append("<i>")
            case "blockquote_close":
                out.append("</i>")
            case "hr":
                out.append('<span foreground="#4b5263">' + ("─" * 40) + "</span>\n\n")
            # ── GFM extras ─────────────────────────────────────────
            case "s_open":
                out.append('<span strikethrough="true">')
            case "s_close":
                out.append("</span>")
            # ── Tables ─────────────────────────────────────────────
            # Pango has no real table widget inside a Label, so we
            # render tables as monospace ASCII boxes. While walking
            # we accumulate cell text into `table_state[-1]` and emit
            # the formatted table on `table_close`.
            case "table_open":
                table_state.append({"rows": [], "current": None, "align": []})
            case "table_close":
                state = table_state.pop()
                out.append(self._render_table(state))
            case "thead_open" | "thead_close" | "tbody_open" | "tbody_close":
                pass
            case "tr_open":
                if table_state:
                    table_state[-1]["current"] = []
            case "tr_close":
                if table_state and table_state[-1]["current"] is not None:
                    table_state[-1]["rows"].append(table_state[-1]["current"])
                    table_state[-1]["current"] = None
            case "th_open" | "td_open":
                if table_state:
                    table_state[-1]["_cell_start"] = len(out)
                    align = (tok.attrGet("style") or "")
                    if "text-align:right" in align:
                        table_state[-1]["_cell_align"] = "right"
                    elif "text-align:center" in align:
                        table_state[-1]["_cell_align"] = "center"
                    else:
                        table_state[-1]["_cell_align"] = "left"
            case "th_close" | "td_close":
                if table_state:
                    state = table_state[-1]
                    start = state.pop("_cell_start", len(out))
                    cell_markup = "".join(out[start:])
                    del out[start:]
                    state["current"].append(
                        {"markup": cell_markup, "align": state.pop("_cell_align", "left")}
                    )
            case _:
                log.debug("unhandled token: %s", tok.type)

    # ── Table rendering ──────────────────────────────────────────────

    # Strips Pango span markup to measure visible cell width. Tables
    # are rendered in monospace so column alignment hinges on char
    # count, not pixel width — accurate enough for agent answers.
    _TAG_STRIP_RE = re.compile(r"<[^>]+>")

    @classmethod
    def _visible_len(cls, markup: str) -> int:
        """Character width of a Pango-markup cell once tags are
        stripped. `&amp;` etc. count as one glyph."""
        bare = cls._TAG_STRIP_RE.sub("", markup)
        bare = (
            bare.replace("&amp;", "&")
            .replace("&lt;", "<")
            .replace("&gt;", ">")
            .replace("&quot;", '"')
            .replace("&apos;", "'")
            .replace("&#x27;", "'")
        )
        return len(bare)

    @classmethod
    def _pad_cell(cls, markup: str, width: int, align: str) -> str:
        pad = max(0, width - cls._visible_len(markup))
        if pad == 0:
            return markup
        space = " " * pad
        if align == "right":
            return space + markup
        if align == "center":
            left = pad // 2
            right = pad - left
            return " " * left + markup + " " * right
        return markup + space

    def _render_table(self, state: dict) -> str:
        """Format an accumulated `{rows: [[{markup, align}]]}` into a
        monospace ASCII-box Pango markup fragment. First row is the
        header; a `─` separator follows it."""
        rows: list[list[dict]] = state.get("rows") or []
        if not rows:
            return ""
        col_count = max(len(r) for r in rows)
        widths = [0] * col_count
        for row in rows:
            for i, cell in enumerate(row):
                widths[i] = max(widths[i], self._visible_len(cell["markup"]))
        lines: list[str] = []
        for idx, row in enumerate(rows):
            padded = []
            for i in range(col_count):
                cell = row[i] if i < len(row) else {"markup": "", "align": "left"}
                padded.append(self._pad_cell(cell["markup"], widths[i], cell["align"]))
            lines.append(" │ ".join(padded))
            if idx == 0:
                lines.append("─┼─".join("─" * w for w in widths))
        body = "\n".join(lines)
        return f'<span font_family="monospace">{body}</span>\n\n'
