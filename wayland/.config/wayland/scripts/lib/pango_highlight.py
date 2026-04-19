"""Pango-flavoured Pygments formatter for fenced code blocks.

`pilot.py` feeds rendered markdown into `Gtk.Label.set_markup()`, which
eats Pango markup (an XML-ish subset that supports `<span foreground=…>`
colouring but knows nothing about CSS classes / HTML structure). None of
Pygments' built-in formatters target that surface directly, so we ship
our own: walk the token stream, pick a onedarker-palette colour per
token type, wrap with a Pango `<span foreground="#…">`.

The public entry point is `highlight_code(source, language)` which
returns a ready-to-splice Pango markup string. It's deliberately
crash-proof — when pygments is missing at import time, when the language
isn't recognised, when the buffer fails to highlight — we fall back to
XML-escaped plain text so the caller can always concatenate us into a
larger markup document."""

from __future__ import annotations

from html import escape as _xml_escape
from io import StringIO
from typing import Optional

try:  # pygments is optional at runtime; see `highlight_code` fallback.
    from pygments.formatter import Formatter  # type: ignore[import-not-found]
    from pygments.token import Token  # type: ignore[import-not-found]

    _PYGMENTS_AVAILABLE = True
except ImportError:  # pragma: no cover — best-effort import guard
    Formatter = object  # type: ignore[assignment,misc]
    Token = None  # type: ignore[assignment]
    _PYGMENTS_AVAILABLE = False


# One Dark palette — matches the overlay's onedarker pilot.css tokens so
# fenced code reads as a first-class citizen of the sidebar rather than
# an alien TextMate embed. Keys are pygments token types, walked parent-
# upward to find a match (e.g. `Token.Name.Function.Magic` falls through
# to `Token.Name.Function`, then `Token.Name`, then the `_DEFAULT_FG`).
if _PYGMENTS_AVAILABLE:
    _COLORS = {
        Token.Keyword:                "#c678dd",
        Token.Keyword.Constant:       "#d19a66",
        Token.Keyword.Declaration:    "#c678dd",
        Token.Keyword.Namespace:      "#c678dd",
        Token.Keyword.Pseudo:         "#c678dd",
        Token.Keyword.Reserved:       "#c678dd",
        Token.Keyword.Type:           "#e5c07b",
        Token.Name.Builtin:           "#e5c07b",
        Token.Name.Builtin.Pseudo:    "#e5c07b",
        Token.Name.Function:          "#61afef",
        Token.Name.Function.Magic:    "#61afef",
        Token.Name.Class:             "#e5c07b",
        Token.Name.Decorator:         "#61afef",
        Token.Name.Exception:         "#e5c07b",
        Token.Name.Namespace:         "#e5c07b",
        Token.Name.Attribute:         "#d19a66",
        Token.Name.Tag:               "#e06c75",
        Token.Name.Variable:          "#e06c75",
        Token.Name.Constant:          "#d19a66",
        Token.String:                 "#98c379",
        Token.String.Doc:             "#5c6370",
        Token.String.Escape:          "#56b6c2",
        Token.String.Interpol:        "#56b6c2",
        Token.Literal:                "#d19a66",
        Token.Number:                 "#d19a66",
        Token.Comment:                "#5c6370",
        Token.Comment.Preproc:        "#c678dd",
        Token.Operator:               "#56b6c2",
        Token.Operator.Word:          "#c678dd",
        Token.Punctuation:            "#abb2bf",
        Token.Generic.Deleted:        "#e06c75",
        Token.Generic.Inserted:       "#98c379",
        Token.Generic.Heading:        "#61afef",
        Token.Generic.Subheading:     "#61afef",
        Token.Generic.Emph:           "#c678dd",
        Token.Generic.Strong:         "#e5c07b",
        Token.Name:                   "#abb2bf",
        Token.Text:                   "#abb2bf",
    }
else:
    _COLORS = {}

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
    """Pygments Formatter that emits one `<span foreground="#…">` per
    token. We don't wrap with `<tt>` / set monospace here — the caller
    already wraps the whole code block with `font_family="monospace"`."""

    name = "Pango"
    aliases = ["pango"]
    filenames: list[str] = []

    def format(self, tokensource, outfile):  # type: ignore[override]
        for ttype, value in tokensource:
            if not value:
                continue
            colour = _token_color(ttype)
            outfile.write(
                f'<span foreground="{colour}">{_xml_escape(value)}</span>'
            )


def highlight_code(source: str, language: Optional[str]) -> str:
    """Return a Pango-markup block for `source` tagged with `language`.

    Falls back to plain escaped text when the language is unknown or
    pygments isn't available. Output is safe to splice into a larger
    Pango markup document — we escape every text run before emitting.
    """
    if not _PYGMENTS_AVAILABLE:
        return _xml_escape(source)

    try:
        from pygments import highlight  # type: ignore[import-not-found]
        from pygments.lexers import (  # type: ignore[import-not-found]
            TextLexer,
            get_lexer_by_name,
            guess_lexer,
        )
        from pygments.util import ClassNotFound  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover — matches the top-level guard
        return _xml_escape(source)

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
    except Exception:
        # Pygments very occasionally raises on malformed input (e.g.
        # partial streams mid-token). Degrade to plain escaped text so
        # the caller's markup stays valid.
        return _xml_escape(source)

    return buf.getvalue()
