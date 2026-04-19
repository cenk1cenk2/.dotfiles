#!/usr/bin/env python3
"""ask — GTK4 layer-shell sidebar that streams a conversational AI response.

Right-side full-height overlay with a markdown scroller and a compose entry
at the bottom. Reads initial text from stdin or clipboard, sends it as the
first user turn, and streams chunks back via a `ConversationAdapter`. A
Unix-socket session lets subsequent invocations forward follow-up turns
into the live window instead of opening a new one."""

import argparse
import errno
import json
import logging
import os
import socket
import subprocess
import sys
import threading
from typing import Optional

from markdown_it import MarkdownIt

from lib import (
    DEFAULT_CONVERSE_ADAPTER,
    ConversationAdapter,
    ConversationAdapterClaude,
    ConversationAdapterCodex,
    ConversationAdapterHttp,
    ConversationProvider,
    InputAdapterClipboard,
    InputAdapterStdin,
    InputMode,
    load_prompt,
    load_relative_file,
    signal_waybar,
)

# gtk4-layer-shell must be LD_PRELOAD'd at program start: its libwayland
# shim hooks in at load time, so without it `is_supported()` returns false
# and every layer-shell call becomes a no-op — the window falls through to
# a normal xdg_toplevel. Re-exec ourselves with the preload if needed —
# only for `toggle`; waybar-poll commands (status / is-running / kill)
# don't open a window and don't need the preload or GTK display.
_LAYER_SHELL_SONAME = "libgtk4-layer-shell.so.0"

def _ensure_layer_shell_preload() -> None:
    current = os.environ.get("LD_PRELOAD", "")
    if _LAYER_SHELL_SONAME in current.split(":"):
        return
    env = os.environ.copy()
    env["LD_PRELOAD"] = (
        f"{current}:{_LAYER_SHELL_SONAME}" if current else _LAYER_SHELL_SONAME
    )
    os.execvpe(sys.executable, [sys.executable, __file__, *sys.argv[1:]], env)

if len(sys.argv) > 1 and sys.argv[1] == "toggle":
    _ensure_layer_shell_preload()

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import (  # type: ignore[attr-defined]  # noqa: E402
    Gdk,
    Gio,
    GLib,
    Gtk,
    Gtk4LayerShell,
    Pango,
)

log = logging.getLogger("ask")

APP_ID = "dev.kilic.wayland.ask"
SOCKET_PATH = os.path.join(
    os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}",
    "wayland-ask.sock",
)

AI_SYSTEM_PROMPT = load_prompt("ask.md", relative_to=__file__)

def _signal_waybar_safe() -> None:
    """Nudge waybar's `custom/ask` module to re-read status. Non-fatal —
    waybar-signal.sh silently ignores unknown modules, and we shouldn't
    let waybar being unavailable take down the overlay."""
    try:
        signal_waybar("ask")
    except Exception as e:
        log.debug("waybar signal failed: %s", e)

def _focused_monitor_name() -> Optional[str]:
    """Ask the compositor for the connector name of the focused output
    (e.g., 'DP-1', 'HDMI-A-1'). Returns None if neither Hyprland nor
    sway is available or neither answers cleanly."""
    try:
        out = subprocess.run(
            ["hyprctl", "monitors", "-j"],
            capture_output=True,
            text=True,
            check=True,
            timeout=1,
        ).stdout
        for m in json.loads(out):
            if m.get("focused"):
                return m.get("name")
    except (
        FileNotFoundError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
    ):
        pass
    try:
        out = subprocess.run(
            ["swaymsg", "-t", "get_outputs"],
            capture_output=True,
            text=True,
            check=True,
            timeout=1,
        ).stdout
        for o in json.loads(out):
            if o.get("focused"):
                return o.get("name")
    except (
        FileNotFoundError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
    ):
        pass

    return None

def _focused_gdk_monitor():
    """Resolve the compositor-focused output to a `GdkMonitor` by
    matching connector names. Returns None on miss."""
    name = _focused_monitor_name()
    display = Gdk.Display.get_default()
    if display is None or name is None:
        return None
    monitors = display.get_monitors()
    for i in range(monitors.get_n_items()):
        monitor = monitors.get_item(i)
        if monitor.get_connector() == name:
            return monitor

    return None

class MarkdownMarkup:
    """Render CommonMark to a Pango markup string for Gtk.Label.

    We swapped from a TextView+TextBuffer+TextTags pipeline to emitting a
    Pango markup string that feeds `Gtk.Label.set_markup()`. Reasons:

    * `Gtk.Label` lays out synchronously during `measure()`, so a freshly
      populated card reports its true natural-height on the first layout
      pass. TextView defers validation and returns a single-line natural
      height until the buffer has been walked post-realize, which is
      what made user cards render collapsed until the assistant reply
      forced a re-layout.
    * `<a href="…">` inside Pango markup drives the Label's native
      `activate-link` signal, so link clicks no longer need
      buffer-coordinate math or a custom GestureClick.
    * Streaming stays cheap: each chunk we re-parse + re-emit markup and
      call `label.set_markup()`. Markdown-it runs in microseconds at the
      sizes AI responses produce."""

    HEADING_SIZES = {1: "x-large", 2: "large", 3: "medium"}
    LINK_COLOR = "#61afef"
    CODE_BG = "#17191e"
    INLINE_CODE_BG = "#2c333d"
    FG_COLOR = "#abb2bf"

    def __init__(self):
        self._md = MarkdownIt("commonmark")

    def render(self, text: str) -> str:
        """Parse `text` as CommonMark and return Pango markup. Safe to
        feed straight to `Gtk.Label.set_markup()`."""
        tokens = self._md.parse(text)
        out: list[str] = []
        self._walk(tokens, out, list_stack=[])
        markup = "".join(out).rstrip(" \n\t")

        return markup

    def _esc(self, text: str) -> str:
        # Pango markup is XML-flavoured; escape every `&`, `<`, `>` in
        # user-supplied content. Quotes only matter inside attribute
        # values but escape_text handles them anyway.
        return GLib.markup_escape_text(text)

    def _walk(self, tokens, out, list_stack) -> None:
        for tok in tokens:
            self._handle(tok, out, list_stack)

    def _handle(self, tok, out, list_stack) -> None:  # noqa: C901
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
                self._walk(tok.children or [], out, list_stack)
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
                out.append(
                    f'<span font_family="monospace" '
                    f'background="{self.CODE_BG}">'
                    f"{self._esc(content)}</span>\n\n"
                )
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
                out.append(
                    '<span foreground="#4b5263">'
                    + ("─" * 40)
                    + "</span>\n\n"
                )
            case _:
                log.debug("unhandled token: %s", tok.type)

class ComposeView:
    """Multi-line compose with an obvious visual box, hint line, and a
    clickable SEND button. Enter submits, Shift+Enter inserts a newline,
    Ctrl+P paste is wired from the window via `append_text`. Auto-grows
    up to `set_max_content_fraction` of the window height before
    scrolling kicks in."""

    # Minimum rows of compose height so the surface stays usable even
    # on tiny screens where 25% of the height wouldn't be enough.
    MIN_ROWS = 2

    def __init__(self, on_submit=None):
        self._on_submit = on_submit

        self.widget = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.widget.add_css_class("ask-compose-wrap")

        self._scroller = Gtk.ScrolledWindow()
        self._scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._scroller.set_propagate_natural_height(True)
        self._scroller.add_css_class("ask-compose")

        self._textview = Gtk.TextView(
            wrap_mode=Gtk.WrapMode.WORD_CHAR,
            top_margin=10,
            bottom_margin=10,
            left_margin=12,
            right_margin=12,
            accepts_tab=False,
        )
        self._textview.add_css_class("ask-compose-text")
        self._scroller.set_child(self._textview)
        self.widget.append(self._scroller)

        # Cache line-height in pixels (Pango metrics unit → pixels).
        # `set_max_content_fraction` later converts a monitor height into
        # a usable row count using this.
        metrics = self._textview.get_pango_context().get_metrics(None)
        self._line_px = (metrics.get_ascent() + metrics.get_descent()) / Pango.SCALE
        self._pad_px = 22
        self._scroller.set_min_content_height(
            int(self._line_px * self.MIN_ROWS) + self._pad_px
        )
        # Fallback cap until the first monitor-bind call fires.
        self._scroller.set_max_content_height(
            int(self._line_px * 6) + self._pad_px
        )

    def set_max_content_fraction(self, window_height_px: int, fraction: float) -> None:
        """Cap the compose scroller at `fraction` of the overlay's total
        height. The overlay is anchored top+bottom so its height equals
        the monitor's geometry height; callers pass that in."""
        cap = int(window_height_px * fraction)
        floor = int(self._line_px * self.MIN_ROWS) + self._pad_px
        self._scroller.set_max_content_height(max(floor, cap))

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar.add_css_class("ask-compose-bar")
        hint = Gtk.Label(
            label="Enter to send  ·  Shift+Enter newline  ·  Ctrl+P paste",
            xalign=0.0,
            hexpand=True,
        )
        hint.add_css_class("ask-compose-hint")
        bar.append(hint)

        self._send_btn = Gtk.Button(label="⏎ send")
        self._send_btn.add_css_class("ask-compose-send")
        self._send_btn.set_tooltip_text("Send the current message (Enter)")
        self._send_btn.connect("clicked", lambda _b: self._submit())
        bar.append(self._send_btn)
        self.widget.append(bar)

        key = Gtk.EventControllerKey()
        key.connect("key-pressed", self._on_key)
        self._textview.add_controller(key)

    def get_text(self) -> str:
        buf = self._textview.get_buffer()

        return buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)

    def set_text(self, text: str) -> None:
        self._textview.get_buffer().set_text(text)

    def append_text(self, text: str) -> None:
        buf = self._textview.get_buffer()
        existing = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)
        joiner = "" if (not existing or existing.endswith((" ", "\n"))) else " "
        buf.insert(buf.get_end_iter(), f"{joiner}{text}")

    def clear(self) -> None:
        self.set_text("")

    def focus(self) -> None:
        self._textview.grab_focus()

    def set_sensitive(self, sensitive: bool) -> None:
        self._textview.set_sensitive(sensitive)
        self._send_btn.set_sensitive(sensitive)

    def _submit(self) -> None:
        text = self.get_text().strip()
        if text and self._on_submit:
            self.clear()
            self._on_submit(text)

    def _on_key(self, _controller, keyval, _keycode, state) -> bool:
        if keyval not in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            return False
        if state & Gdk.ModifierType.SHIFT_MASK:
            # Shift+Enter: default handler inserts a newline, grow the box.
            return False
        self._submit()

        return True

class QueueRow(Gtk.ListBoxRow):
    """A queued turn rendered as a full-width card. The message wraps
    freely (no truncation preview) and three labelled buttons live in
    an action strip at the bottom: `✎ edit` toggles an inline multi-line
    editor, `⏎ send` promotes the message to the next slot, `✕ drop`
    removes it. In edit mode, Ctrl+Enter commits; the edit button
    relabels to `✓ save` while editing."""

    def __init__(self, text: str, on_send, on_remove, on_edit_commit):
        super().__init__()
        self._text = text
        self._on_send = on_send
        self._on_remove = on_remove
        self._on_edit_commit = on_edit_commit
        self._editing = False
        self._edit_scroller: Optional[Gtk.ScrolledWindow] = None
        self._edit_textview: Optional[Gtk.TextView] = None
        self.add_css_class("ask-queue-row")

        self._card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._card.add_css_class("ask-queue-card")

        self._label = Gtk.Label(label=text, xalign=0.0, hexpand=True)
        self._label.set_wrap(True)
        self._label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self._label.set_selectable(True)
        self._label.add_css_class("ask-queue-text")
        self._card.append(self._label)

        actions = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=4,
            halign=Gtk.Align.END,
        )
        actions.add_css_class("ask-queue-actions")

        self._edit_btn = Gtk.Button(label="✎ edit")
        self._edit_btn.add_css_class("ask-queue-edit")
        self._edit_btn.set_tooltip_text("Edit this message")
        self._edit_btn.connect("clicked", lambda _b: self._toggle_edit())
        actions.append(self._edit_btn)

        send_btn = Gtk.Button(label="⏎ send")
        send_btn.add_css_class("ask-queue-send")
        send_btn.set_tooltip_text("Promote and dispatch this message now")
        send_btn.connect("clicked", lambda _b: self._on_send(self))
        actions.append(send_btn)

        remove_btn = Gtk.Button(label="✕ drop")
        remove_btn.add_css_class("ask-queue-remove")
        remove_btn.set_tooltip_text("Remove this message from the queue")
        remove_btn.connect("clicked", lambda _b: self._on_remove(self))
        actions.append(remove_btn)

        self._card.append(actions)
        self.set_child(self._card)

    def text(self) -> str:
        return self._text

    def _toggle_edit(self) -> None:
        if self._editing:
            self._commit_edit()
        else:
            self._enter_edit_mode()

    def _enter_edit_mode(self) -> None:
        self._editing = True
        self._card.remove(self._label)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_propagate_natural_height(True)
        scroller.set_max_content_height(160)
        scroller.add_css_class("ask-queue-edit")

        textview = Gtk.TextView(
            wrap_mode=Gtk.WrapMode.WORD_CHAR,
            top_margin=6,
            bottom_margin=6,
            left_margin=8,
            right_margin=8,
        )
        textview.add_css_class("ask-queue-edit-text")
        textview.get_buffer().set_text(self._text)
        scroller.set_child(textview)

        key = Gtk.EventControllerKey()
        key.connect("key-pressed", self._on_edit_key)
        textview.add_controller(key)

        self._card.prepend(scroller)
        textview.grab_focus()
        self._edit_scroller = scroller
        self._edit_textview = textview
        self._edit_btn.set_label("✓ save")

    def _on_edit_key(self, _controller, keyval, _keycode, state) -> bool:
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and (
            state & Gdk.ModifierType.CONTROL_MASK
        ):
            self._commit_edit()
            return True

        return False

    def _commit_edit(self) -> None:
        if not self._editing or self._edit_textview is None:
            return
        buf = self._edit_textview.get_buffer()
        new_text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True).strip()
        self._editing = False
        if self._edit_scroller is not None:
            self._card.remove(self._edit_scroller)
        self._edit_scroller = None
        self._edit_textview = None
        if new_text:
            self._text = new_text
        self._label.set_label(self._text)
        self._card.prepend(self._label)
        self._edit_btn.set_label("✎ edit")
        self._on_edit_commit(self, self._text)

class TurnCard:
    """One turn in the conversation. `role` is 'user' or 'assistant'.
    User cards get populated once via `set_text`; assistant cards stream
    chunk-by-chunk via `append`. Backed by `Gtk.Label` with Pango markup
    — labels measure synchronously so cards size correctly on the first
    layout pass (TextView doesn't, which used to leave user cards
    collapsed until the assistant reply forced a re-layout)."""

    def __init__(self, role: str, title: str, on_link):
        self.role = role
        self.widget = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        self.widget.add_css_class("ask-card")
        self.widget.add_css_class(f"ask-card-{role}")

        role_label = Gtk.Label(label=title, xalign=0.0)
        role_label.add_css_class("ask-card-role")
        role_label.add_css_class(f"ask-card-role-{role}")
        self.widget.append(role_label)

        self._label = Gtk.Label(
            xalign=0.0,
            yalign=0.0,
            hexpand=True,
            wrap=True,
            wrap_mode=Pango.WrapMode.WORD_CHAR,
            use_markup=True,
            selectable=True,
            # Prefer wrapping at word boundaries where possible, fall
            # back to inline when a single long token would overflow.
            natural_wrap_mode=Gtk.NaturalWrapMode.WORD,
        )
        self._label.add_css_class("ask-card-text")
        # Fire the window's link handler when the user clicks a rendered
        # `<a href="…">`. Return True to tell GTK "we handled it" — we
        # don't want the default xdg-open path because the handler may
        # want to route through a compositor-specific opener.
        self._label.connect(
            "activate-link",
            lambda _lbl, uri: (on_link(uri), True)[1],
        )
        self.widget.append(self._label)

        self._md = MarkdownMarkup()
        self._text = ""
        self._on_link = on_link

    def append(self, chunk: str) -> None:
        self._text += chunk
        self._label.set_markup(self._md.render(self._text))

    def set_text(self, text: str) -> None:
        self._text = text
        self._label.set_markup(self._md.render(text))

    def get_text(self) -> str:
        return self._text

class AskWindow(Gtk.ApplicationWindow):
    """Layer-shell sidebar. Conversation is a vertical stack of TurnCard
    widgets (one per user/assistant turn), queued turns are cards of
    their own above the compose, and compose is a multi-line TextView
    with a visible SEND button. Phases (idle/pending/streaming) surface
    in both the header pill and the waybar module."""

    USER_TITLE = "Retarded Peasant"
    ASSISTANT_TITLE_FMT = "AI Overlord - {provider}"
    ASSISTANT_TITLE_WITH_MODEL_FMT = "AI Overlord - {provider} ({model})"
    HEADER_FMT = "Ask - {provider}"
    HEADER_WITH_MODEL_FMT = "Ask - {provider} ({model})"

    def __init__(self, app: Gtk.Application, adapter: ConversationAdapter):
        super().__init__(application=app, title="Ask")
        # `.overlay` is our root scope — every CSS rule is namespaced under
        # it so theme selectors like `window { background: … }` can't win.
        self.add_css_class("overlay")
        self._adapter = adapter
        # HTTP adapter carries a model; claude/codex wrap CLIs that pick
        # their own. Model string can be empty → treat as absent.
        raw_model = getattr(adapter, "model", "") or ""
        self._model: Optional[str] = raw_model.strip() or None
        self._provider_name = adapter.provider.value
        self._streaming = False
        self._alive = True
        self._queue: list[QueueRow] = []
        self._phase: str = "idle"
        self._cards: list[TurnCard] = []
        self._active_assistant: Optional[TurnCard] = None
        self._install_css()

        Gtk4LayerShell.init_for_window(self)
        # Explicit namespace so compositor layerrules can target us
        # (`layerrule = blur, ask`) without regex-escaping the app-id.
        Gtk4LayerShell.set_namespace(self, "ask")
        Gtk4LayerShell.set_layer(self, Gtk4LayerShell.Layer.TOP)
        Gtk4LayerShell.set_anchor(self, Gtk4LayerShell.Edge.TOP, True)
        Gtk4LayerShell.set_anchor(self, Gtk4LayerShell.Edge.BOTTOM, True)
        Gtk4LayerShell.set_anchor(self, Gtk4LayerShell.Edge.RIGHT, True)
        # EXCLUSIVE so toggle-window / present() grab keyboard focus the
        # moment we map. Escape hides → grab releases automatically.
        Gtk4LayerShell.set_keyboard_mode(self, Gtk4LayerShell.KeyboardMode.EXCLUSIVE)
        self.set_default_size(self._overlay_width(), -1)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.add_css_class("ask-root")

        # Header --------------------------------------------------------
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        header.add_css_class("ask-header")
        self._provider_label = Gtk.Label(
            label=self._header_title(),
            xalign=0.0,
            hexpand=True,
        )
        self._provider_label.add_css_class("ask-provider")
        self._provider_label.add_css_class("idle")
        close_btn = Gtk.Button(label="✕")
        close_btn.add_css_class("ask-close")
        close_btn.connect("clicked", lambda _b: self.close())
        header.append(self._provider_label)
        header.append(close_btn)
        root.append(header)

        # Conversation: a vertical box of TurnCard widgets inside a
        # scroller. Each card is its own markdown-rendered surface.
        self._conv_scroller = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        # NEVER horizontal so the child Box fills the viewport width
        # instead of collapsing to its minimum natural width — that was
        # making cards render as narrow slivers (sometimes invisible
        # entirely when the short role label was the only measurable
        # natural-width content).
        self._conv_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._conv_scroller.add_css_class("ask-conv-scroller")
        self._conv_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        self._conv_box.add_css_class("ask-conv")
        self._conv_scroller.set_child(self._conv_box)
        root.append(self._conv_scroller)

        # Auto-scroll-when-pinned. We track whether the vadjustment is at
        # the bottom whenever the user actively scrolls (`value-changed`).
        # When content grows (`upper` increases — a new card was appended
        # or a streaming chunk landed), if we were pinned we jump to the
        # new bottom. This keeps streams in view without stealing the
        # scroll position from a user who's scrolled up to read earlier
        # turns.
        self._pinned = True
        vadj = self._conv_scroller.get_vadjustment()
        vadj.connect("value-changed", self._on_vadj_value_changed)
        vadj.connect("notify::upper", self._on_vadj_upper_changed)

        # Queue ---------------------------------------------------------
        self._queue_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._queue_box.add_css_class("ask-queue")
        queue_header = Gtk.Label(label="QUEUED", xalign=0.0)
        queue_header.add_css_class("ask-queue-header")
        self._queue_box.append(queue_header)
        self._queue_listbox = Gtk.ListBox()
        self._queue_listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self._queue_box.append(self._queue_listbox)
        self._queue_box.set_visible(False)
        root.append(self._queue_box)

        # Compose -------------------------------------------------------
        self._compose = ComposeView(on_submit=self.dispatch_turn)
        root.append(self._compose.widget)

        self.set_child(root)

        self._wire_keys()
        self.connect("close-request", self._on_close_request)

    # -- Public API -----------------------------------------------------

    def focus_compose(self) -> None:
        self._compose.focus()

    def toggle_visibility(self) -> bool:
        if self.get_visible():
            self.set_visible(False)
        else:
            # Only re-home when we're actually coming back from hidden:
            # user may have moved to a different monitor, changed output
            # scale, etc. between hides. No-op while we're already shown.
            self._bind_to_focused_monitor()
            self.set_visible(True)
            self.present()
            self._compose.focus()

        return False

    def dispatch_turn(self, user_message: str) -> None:
        message = user_message.strip()
        if not message:
            return
        if not self.get_visible():
            # Coming back from hidden — re-home to whichever output the
            # user is looking at NOW, same policy as manual toggle.
            self._bind_to_focused_monitor()
            self.set_visible(True)
            self.present()
        if self._streaming or self._queue:
            # Manual-drain queue semantics: anything pending stays put
            # until the user hits a card's ⏎. If there's already a
            # stream in flight OR the queue is non-empty, new incoming
            # turns join the tail instead of jumping ahead.
            self._enqueue(message)
            return
        self._start_turn(message)

    def phase(self) -> str:
        return self._phase

    def is_streaming(self) -> bool:
        return self._streaming

    def queue_size(self) -> int:
        return len(self._queue)

    # -- Turn lifecycle -------------------------------------------------

    def _start_turn(self, message: str) -> None:
        self._append_user_card(message)
        self._active_assistant = self._append_assistant_card()
        self._streaming = True
        # Compose stays enabled: the queue system handles anything the
        # user types while this turn is streaming — new submissions get
        # pushed behind the current stream automatically.
        self._set_phase("pending")
        threading.Thread(target=self._run_turn, args=(message,), daemon=True).start()

    def _append_user_card(self, text: str) -> TurnCard:
        card = TurnCard(
            role="user",
            title=self.USER_TITLE,
            on_link=self._open_link,
        )
        self._cards.append(card)
        # Explicit user action → always pin-to-bottom and scroll, even
        # if the user had scrolled up before clicking send.
        self._pinned = True
        self._conv_box.append(card.widget)
        card.set_text(text)

        return card

    def _append_assistant_card(self) -> TurnCard:
        card = TurnCard(
            role="assistant",
            title=self._assistant_title(),
            on_link=self._open_link,
        )
        self._cards.append(card)
        self._conv_box.append(card.widget)

        return card

    def _header_title(self) -> str:
        if self._model:
            return self.HEADER_WITH_MODEL_FMT.format(
                provider=self._provider_name, model=self._model
            )

        return self.HEADER_FMT.format(provider=self._provider_name)

    def _assistant_title(self) -> str:
        if self._model:
            return self.ASSISTANT_TITLE_WITH_MODEL_FMT.format(
                provider=self._provider_name, model=self._model
            )

        return self.ASSISTANT_TITLE_FMT.format(provider=self._provider_name)

    def _append_chunk(self, chunk: str) -> bool:
        """Main-thread-safe sink for adapter chunks. Appends to the
        currently-streaming assistant card; the `notify::upper` hook on
        the vadjustment keeps us scrolled to bottom whenever we're
        pinned. Returns False so it composes with `GLib.idle_add`."""
        if self._active_assistant is not None:
            self._active_assistant.append(chunk)

        return False

    def _run_turn(self, user_message: str) -> None:
        first_chunk = True
        try:
            for chunk in self._adapter.turn(user_message):
                if not self._alive:
                    return
                if first_chunk:
                    # First delta arrived — leave red `pending`, go yellow
                    # `streaming`.
                    GLib.idle_add(self._set_phase, "streaming")
                    first_chunk = False
                GLib.idle_add(self._append_chunk, chunk)
        except Exception as e:
            if not self._alive:
                return
            log.error("turn failed: %s", e)
            GLib.idle_add(self._append_chunk, f"\n\n*error: {e}*\n")
        finally:
            if self._alive:
                GLib.idle_add(self._mark_idle)

    def _mark_idle(self) -> bool:
        self._streaming = False
        self._active_assistant = None
        if self._alive:
            # Compose was never disabled; just reclaim focus so the user
            # can continue typing without clicking. Queued items stay
            # put — user controls when each one goes via the card's ⏎.
            self._compose.focus()
            self._set_phase("idle")

        return False

    # -- Phase colouring -----------------------------------------------

    def _set_phase(self, phase: str) -> bool:
        self._phase = phase
        for cls in ("idle", "pending", "streaming"):
            if cls == phase:
                self._provider_label.add_css_class(cls)
            else:
                self._provider_label.remove_css_class(cls)
        _signal_waybar_safe()

        return False

    # -- Queue ----------------------------------------------------------

    def _enqueue(self, message: str) -> None:
        row = QueueRow(
            text=message,
            on_send=self._on_queue_send,
            on_remove=self._on_queue_remove,
            on_edit_commit=self._on_queue_edit,
        )
        self._queue.append(row)
        self._queue_listbox.append(row)
        self._queue_box.set_visible(True)
        _signal_waybar_safe()

    def _pop_queue_front(self) -> Optional[str]:
        if not self._queue:
            return None
        row = self._queue.pop(0)
        self._queue_listbox.remove(row)
        if not self._queue:
            self._queue_box.set_visible(False)
        _signal_waybar_safe()

        return row.text()

    def _remove_queue_row(self, row: QueueRow) -> Optional[str]:
        if row not in self._queue:
            return None
        self._queue.remove(row)
        self._queue_listbox.remove(row)
        if not self._queue:
            self._queue_box.set_visible(False)
        _signal_waybar_safe()

        return row.text()

    def _on_queue_send(self, row: QueueRow) -> None:
        # Manual-drain policy: ⏎ dispatches this specific card only if
        # nothing is currently streaming. While streaming, the button is
        # a soft no-op — the user can wait or use the ⏎ on another
        # card later. Keeps the conversation's pacing in their hands.
        if self._streaming:
            log.info("ignoring queue-send while streaming")
            return
        text = self._remove_queue_row(row)
        if not text:
            return
        self._start_turn(text)

    def _on_queue_remove(self, row: QueueRow) -> None:
        self._remove_queue_row(row)

    def _on_queue_edit(self, _row: QueueRow, _new_text: str) -> None:
        # Row keeps its slot; the card shows the updated text. Nothing
        # else to do — dispatch_turn reads row.text() on drain / send.
        pass

    # -- Scroll / keys / links -----------------------------------------

    _PIN_THRESHOLD_PX = 24

    def _on_vadj_value_changed(self, adj) -> None:
        """User-initiated scroll updates the pinned flag. If they scroll
        to within `_PIN_THRESHOLD_PX` of the bottom we consider them
        pinned; scrolling up beyond that unpins so subsequent content
        arrivals don't fight the reader."""
        bottom = adj.get_upper() - adj.get_page_size()
        self._pinned = adj.get_value() >= bottom - self._PIN_THRESHOLD_PX

    def _on_vadj_upper_changed(self, adj, _pspec) -> None:
        """Content grew (card appended or streaming chunk landed). If
        we were pinned, stick to the new bottom."""
        if self._pinned:
            adj.set_value(max(0, adj.get_upper() - adj.get_page_size()))

    def _open_link(self, url: str) -> None:
        Gio.AppInfo.launch_default_for_uri(url, None)

    def _wire_keys(self) -> None:
        # CAPTURE phase so the window sees keys before any focused child
        # controller. Without it, Home/End/PgUp/PgDn would be consumed by
        # the compose TextView's built-in key handling (cursor moves)
        # before the conversation scroller ever got a chance to react.
        key = Gtk.EventControllerKey()
        key.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key.connect("key-pressed", self._on_key)
        self.add_controller(key)

    def _on_key(self, _controller, keyval, _keycode, state) -> bool:
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        if ctrl and keyval == Gdk.KEY_q:
            self.close()
            return True
        if ctrl and keyval == Gdk.KEY_p:
            self._paste_clipboard_into_compose()
            return True
        if ctrl and keyval == Gdk.KEY_d:
            self._cancel_current_turn()
            return True
        if ctrl and keyval == Gdk.KEY_f:
            self._compose.focus()
            return True
        if keyval == Gdk.KEY_Home:
            self._scroll_to(0.0)
            return True
        if keyval == Gdk.KEY_End:
            self._scroll_to(1.0)
            return True
        if keyval == Gdk.KEY_Page_Up:
            self._scroll_page(-1)
            return True
        if keyval == Gdk.KEY_Page_Down:
            self._scroll_page(1)
            return True
        if keyval == Gdk.KEY_Escape:
            self.set_visible(False)
            return True

        return False

    def _scroll_to(self, fraction: float) -> None:
        """Jump to `fraction` of the scroll range. 0.0 = top, 1.0 = bottom.
        Updates `_pinned` so the auto-follow state matches the landing
        position."""
        adj = self._conv_scroller.get_vadjustment()
        bottom = max(0.0, adj.get_upper() - adj.get_page_size())
        target = adj.get_lower() + (bottom - adj.get_lower()) * max(0.0, min(1.0, fraction))
        adj.set_value(target)
        self._pinned = target >= bottom - self._PIN_THRESHOLD_PX

    def _scroll_page(self, direction: int) -> None:
        """direction = -1 for PgUp, +1 for PgDn. Steps by one page-size;
        refreshes `_pinned` based on where we landed."""
        adj = self._conv_scroller.get_vadjustment()
        step = adj.get_page_size() * direction
        bottom = max(0.0, adj.get_upper() - adj.get_page_size())
        target = max(adj.get_lower(), min(bottom, adj.get_value() + step))
        adj.set_value(target)
        self._pinned = target >= bottom - self._PIN_THRESHOLD_PX

    def _cancel_current_turn(self) -> None:
        """Ctrl+D: abort the in-flight adapter turn so the user can say
        something else without waiting for the reply to finish. Marks the
        active assistant card with a `(cancelled)` footer so the
        conversation transcript stays honest about what happened."""
        if not self._streaming:
            log.debug("cancel requested with no turn in flight")
            return
        try:
            self._adapter.cancel()
        except Exception as e:
            log.warning("adapter cancel raised: %s", e)
        if self._active_assistant is not None:
            self._active_assistant.append("\n\n*— cancelled —*")

    def _paste_clipboard_into_compose(self) -> None:
        text = InputAdapterClipboard().read() or ""
        if not text:
            return
        self._compose.focus()
        self._compose.append_text(text)

    def _on_close_request(self, _window) -> bool:
        self._alive = False
        try:
            self._adapter.close()
        except Exception as e:
            log.warning("adapter close failed: %s", e)
        _signal_waybar_safe()

        return False

    # -- Statics --------------------------------------------------------

    @staticmethod
    def _overlay_width(fraction: float = 0.4, monitor=None) -> int:
        """Fraction of the focused monitor's logical width. Caller can
        pass a specific `monitor` (already resolved); otherwise we query
        the compositor live. Falls back to GDK's first monitor, then a
        hardcoded default, when no info is available."""
        width = None
        if monitor is None:
            monitor = _focused_gdk_monitor()
        if monitor is not None:
            width = monitor.get_geometry().width
        if width is None:
            display = Gdk.Display.get_default()
            if display is not None:
                monitors = display.get_monitors()
                if monitors.get_n_items() > 0:
                    width = monitors.get_item(0).get_geometry().width
        if not width:
            return 520

        return max(320, int(width * fraction))

    def _bind_to_focused_monitor(self) -> None:
        """Pin the layer-shell surface to whichever output is currently
        focused, resize width to 40% of that output, and recompute the
        compose-scroller cap to 25% of that output's height. Called on
        every toggle-to-visible so monitor switches rehome the overlay.

        The layer-shell surface size follows the widget's requested size.
        GTK caches allocations aggressively, so on a monitor switch we
        have to force a full reconfigure — `set_default_size` + a fresh
        `set_size_request` + `queue_resize`, and if we're currently
        mapped we unmap/remap so the compositor assigns a new layer
        surface with the new dimensions. Without the unmap/remap the
        first width sticks forever."""
        monitor = _focused_gdk_monitor()
        if monitor is not None:
            Gtk4LayerShell.set_monitor(self, monitor)
        width = self._overlay_width(monitor=monitor)
        self.set_default_size(width, -1)
        self.set_size_request(width, -1)
        self.queue_resize()
        if monitor is not None:
            self._compose.set_max_content_fraction(
                monitor.get_geometry().height, 0.25
            )
        if self.get_mapped():
            # Remap to commit a new layer surface with the updated size.
            # Layer-shell only reads the surface dimensions during the
            # initial configure; subsequent widget allocations don't
            # propagate to the compositor.
            self.unmap()
            self.map()

    @staticmethod
    def _install_css() -> None:
        """Load `ask.css` from alongside this script and register it at
        USER+1 priority.

        A USER-priority `~/.config/gtk-4.0/gtk.css` beats
        APPLICATION-priority rules regardless of selector specificity —
        Graphite-style themes installed there ship `textview text {
        background-color: #0F0F0F }` which paints opaque black behind
        every TextView's text subnode in the overlay. `USER + 1` is the
        smallest bump that beats `~/.config/gtk-4.0/gtk.css` without
        stomping on anything the user might layer on top intentionally.

        Parsing errors are routed to our logger so missing selectors /
        bad rule bodies surface in `-v` runs instead of vanishing."""
        css = load_relative_file("ask.css", relative_to=__file__).encode("utf-8")
        provider = Gtk.CssProvider()

        def _on_error(_prov, section, err):
            start = section.get_start_location()
            log.warning(
                "ask.css parse error at line %d col %d: %s",
                start.lines + 1,
                start.line_chars + 1,
                err.message,
            )

        provider.connect("parsing-error", _on_error)
        provider.load_from_data(css, len(css))
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_USER + 1,
        )

def _is_live() -> bool:
    """Probe the session socket without sending a command. Returns True if
    a server accepted our connect, False if the file is stale / absent."""
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(1)
    try:
        probe.connect(SOCKET_PATH)

        return True
    except (ConnectionRefusedError, FileNotFoundError):
        return False
    except OSError as e:
        log.warning("socket probe failed: %s", e)

        return False
    finally:
        probe.close()

def _send(cmd: str, **kwargs) -> Optional[dict]:
    """Send a one-shot JSON command to the running session.

    Returns the parsed response dict, or None when no session answers.
    Stale socket files from a crashed session are unlinked so the next
    invocation can bind fresh."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(2)
    try:
        sock.connect(SOCKET_PATH)
    except (FileNotFoundError, ConnectionRefusedError):
        try:
            os.unlink(SOCKET_PATH)
        except FileNotFoundError:
            pass
        return None
    except OSError as e:
        log.warning("socket connect failed: %s", e)
        return None

    try:
        payload = json.dumps({"cmd": cmd, **kwargs}) + "\n"
        sock.sendall(payload.encode())
        chunks = []
        while True:
            data = sock.recv(4096)
            if not data:
                break
            chunks.append(data)
        raw = b"".join(chunks).decode("utf-8", errors="replace").strip()
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            log.warning("bad response from session: %s (raw=%r)", e, raw)
            return None
    finally:
        sock.close()

class Session:
    """Owns the Unix socket for a live ask window. A background thread
    accepts connections from forwarder invocations and dispatches their
    `turn` / `status` commands back onto the GTK main thread."""

    def __init__(self, window: AskWindow, provider: ConversationProvider):
        self._window = window
        self._provider = provider
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(SOCKET_PATH)
        except OSError as e:
            if e.errno != errno.EADDRINUSE:
                sock.close()
                raise
            # Socket file exists. Probe to tell live owner from stale file:
            # a live owner answers connect; a stale file gives ECONNREFUSED.
            # Only unlink after confirming stale, so we don't race another
            # starting owner into a two-windows-one-path state.
            if _is_live():
                sock.close()
                raise RuntimeError(
                    f"another ask session is already running at {SOCKET_PATH}"
                )
            try:
                os.unlink(SOCKET_PATH)
            except FileNotFoundError:
                pass
            sock.bind(SOCKET_PATH)

        os.chmod(SOCKET_PATH, 0o600)
        sock.listen(4)
        self._sock = sock
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        try:
            os.unlink(SOCKET_PATH)
        except FileNotFoundError:
            pass

    def _serve(self) -> None:
        assert self._sock is not None, "_serve requires start() to have run"
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            raw = conn.recv(8192).decode("utf-8", errors="replace").strip()
            response = self._dispatch(raw)
            try:
                conn.sendall(json.dumps(response).encode())
            except (BrokenPipeError, ConnectionResetError):
                # Client went away before reading our reply. Common and
                # expected: forwarders that fire-and-forget, kill
                # commands that tear everything down before the response
                # can land. Not worth a warning.
                log.debug("client closed before response was delivered")
        except Exception as e:
            log.warning("socket handler error: %s", e)
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _dispatch(self, raw: str) -> dict:
        try:
            obj = json.loads(raw) if raw else {}
            cmd = obj.get("cmd", "")
        except json.JSONDecodeError:
            return {"ok": False, "error": f"bad request: {raw!r}"}

        log.info("socket cmd: %s", cmd)
        match cmd:
            case "turn":
                text = (obj.get("text") or "").strip()
                if text:
                    GLib.idle_add(self._window.dispatch_turn, text)

                return {"ok": True}
            case "status":
                return {
                    "ok": True,
                    "phase": self._window.phase(),
                    "provider": self._provider.value,
                    "queue": self._window.queue_size(),
                }
            case "kill":
                # Tear down from the GTK main thread so close-request handlers
                # fire in the right order. The server socket will be closed by
                # the window's on_close hook.
                GLib.idle_add(self._window.close)

                return {"ok": True}
            case "toggle-window":
                # Hide-if-visible / show-if-hidden, without touching the
                # conversation. Same primitive as Escape + a forwarded turn,
                # exposed so keybinds can drive it from outside the window.
                GLib.idle_add(self._window.toggle_visibility)

                return {"ok": True}
            case _:
                return {"ok": False, "error": f"unhandled command: {cmd!r}"}

def _build_adapter(args) -> ConversationAdapter:
    # Callers pass argparse values through as-is. None / missing values
    # collapse to per-adapter defaults (see `kwargs.get(name) or DEFAULT`
    # in the adapter __init__s), so this function doesn't replicate them.
    provider = ConversationProvider(args.converse_provider)
    match provider:
        case ConversationProvider.HTTP:
            return ConversationAdapterHttp(
                AI_SYSTEM_PROMPT,
                base_url=args.converse_base_url,
                model=args.converse_model,
                api_key=os.environ.get("AI_KILIC_DEV_API_KEY", ""),
                temperature=args.converse_temperature,
                top_p=args.converse_top_p,
                thinking=args.converse_thinking,
                num_ctx=args.converse_num_ctx,
                tool_ids=args.tool_ids or None,
                user_agent="ask/1.0",
            )
        case ConversationProvider.CLAUDE:
            return ConversationAdapterClaude(AI_SYSTEM_PROMPT, model=args.converse_model)
        case ConversationProvider.CODEX:
            return ConversationAdapterCodex(AI_SYSTEM_PROMPT, model=args.converse_model)
        case _:
            raise ValueError(f"unknown converse provider: {provider!r}")

def _read_input(mode: InputMode) -> str:
    match mode:
        case InputMode.STDIN:
            # Only block on stdin when it's actually a pipe. Running
            # `ask.py toggle` from a TTY (or via a compositor bind
            # with no piped input) returns empty so the window opens
            # ready-to-type instead of hanging on `sys.stdin.read()`
            # waiting for EOF that never comes.
            if sys.stdin.isatty():
                text = ""
            else:
                text = InputAdapterStdin().read()
        case InputMode.CLIPBOARD:
            text = InputAdapterClipboard().read()
        case _:
            raise ValueError(f"unknown input mode: {mode!r}")

    return (text or "").strip()

def _cmd_toggle(args) -> None:
    """Unified toggle. Three behaviours, chosen from context:

      1. No session + any input     -> open a new session.
      2. Session + non-empty input  -> forward the input as a turn.
      3. Session + empty input      -> flip overlay visibility,
         *unless* the empty input came from a closed pipe (press-2 of
         a speech toggle pair dumping nothing into us) — in that case
         leave the session alone.
    """
    initial = _read_input(args.input)
    # stdin being a TTY means the user invoked ask.py from a terminal
    # or a compositor bind with nothing piped in; a closed/empty pipe
    # means the upstream process exited without writing (common with
    # press-2 of `speech.py toggle --output stdout | ask.py toggle`).
    piped_empty = args.input == InputMode.STDIN and not sys.stdin.isatty()

    status = _send("status")
    if status and status.get("ok"):
        if initial:
            _send("turn", text=initial)
            return
        if piped_empty:
            # Fire-and-forget callers (speech press-2) — don't touch
            # the visibility; the payload-bearing sibling pipe will
            # reach the session on its own.
            return
        _send("toggle-window")

        return

    adapter = _build_adapter(args)

    app = Gtk.Application(application_id=APP_ID)
    session: dict[str, Optional[Session]] = {"server": None}

    def on_activate(application):
        window = AskWindow(application, adapter)
        server = Session(window, adapter.provider)
        server.start()
        session["server"] = server

        def on_close(_w):
            server.stop()
            _signal_waybar_safe()

            return False

        window.connect("close-request", on_close)
        # Pick the focused monitor BEFORE present() so the first
        # layer-surface configure uses the right output width. The same
        # helper runs on every subsequent toggle-to-visible.
        window._bind_to_focused_monitor()
        window.present()
        window.focus_compose()
        _signal_waybar_safe()
        if initial:
            window.dispatch_turn(initial)

    app.connect("activate", on_activate)
    try:
        app.run([sys.argv[0]])
    finally:
        server = session.get("server")
        if server:
            server.stop()
        _signal_waybar_safe()

def _cmd_status() -> None:
    """Waybar custom-module payload. Compact icon-only text (provider lives
    in the tooltip); class picks the state colour (idle green / pending red
    / streaming yellow); queue depth renders as a Pango superscript badge
    so N pending turns show as `󱍊³` without stealing horizontal space."""
    resp = _send("status")
    if not resp or not resp.get("ok"):
        print(json.dumps({"class": "idle", "text": "", "tooltip": "Ask idle"}))

        return

    provider = resp.get("provider", "")
    phase = resp.get("phase", "idle")
    queue = int(resp.get("queue", 0) or 0)
    icon = "󱍊"
    badge = f"<sup>{queue}</sup>" if queue > 0 else ""
    text = f"{icon}{badge}"
    match phase:
        case "streaming":
            tooltip = f"Ask: streaming via {provider}"
        case "pending":
            tooltip = f"Ask: waiting on first chunk from {provider}"
        case _:
            tooltip = f"Ask: {provider} idle"
    if queue > 0:
        tooltip += f"  ({queue} queued)"
    print(json.dumps({"class": phase, "text": text, "tooltip": tooltip}))

def _cmd_is_running() -> None:
    """Waybar `exec-if` gate. Exit 0 when a session socket is live so the
    custom module shows, otherwise exit 1 and stay hidden."""
    sys.exit(0 if _is_live() else 1)

def _cmd_kill() -> None:
    """End the running ask session (if any) and return immediately. Matches
    `speech.py kill` so recording-mode bindings can terminate either."""
    resp = _send("kill")
    if not resp:
        # No live session answered — clear a stale socket file so the next
        # toggle starts clean.
        try:
            os.unlink(SOCKET_PATH)
        except FileNotFoundError:
            pass
    _signal_waybar_safe()

def main():
    parser = argparse.ArgumentParser(description="Conversational AI sidebar overlay")
    parser.add_argument("-v", "--verbose", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)

    toggle_parser = subparsers.add_parser(
        "toggle",
        help="open the overlay (or forward a turn to a running session)",
    )
    toggle_parser.add_argument(
        "--input",
        type=InputMode,
        choices=[InputMode.STDIN, InputMode.CLIPBOARD],
        default=InputMode.STDIN,
        help="Source of the initial user turn",
    )
    toggle_parser.add_argument(
        "--converse-provider",
        choices=list(ConversationProvider),
        default=DEFAULT_CONVERSE_ADAPTER,
    )
    toggle_parser.add_argument(
        "--converse-base-url",
        default="https://ai.kilic.dev/api/v1",
    )
    # Model default is provider-specific (see `_build_adapter`). A bare
    # `--converse-model` stays None so each adapter picks its own baseline;
    # an explicit value overrides for any provider.
    toggle_parser.add_argument("--converse-model", default=None)
    toggle_parser.add_argument("--converse-temperature", type=float)
    toggle_parser.add_argument("--converse-top-p", type=float)
    toggle_parser.add_argument(
        "--converse-thinking",
        nargs="?",
        const="high",
        default="none",
        choices=["high", "medium", "low", "none"],
    )
    toggle_parser.add_argument("--converse-num-ctx", type=int)
    # OpenWebUI extensions for --converse-provider=http. Ignored by other
    # providers and by plain OpenAI endpoints. Default bundles the
    # always-on built-ins (web_search + memory); pass extra `--tool-id`
    # flags for custom tools or `server:mcp:<id>` for MCP servers.
    toggle_parser.add_argument(
        "--tool-id",
        action="append",
        dest="tool_ids",
        default=["web_search", "memory"],
        metavar="ID",
        help=(
            "server-side tool UUID or built-in pseudo-id "
            "(web_search/memory/code_interpreter/image_generation/voice). "
            "Use 'server:mcp:<id>' for an MCP server. Repeatable. "
            "OpenWebUI-only."
        ),
    )

    subparsers.add_parser("status", help="print waybar-shaped JSON status")
    subparsers.add_parser(
        "is-running",
        help="exit 0 if a session is live, non-zero otherwise",
    )
    subparsers.add_parser("kill", help="terminate the running session (if any)")

    args = parser.parse_args()

    logging.basicConfig(
        format="%(name)s: %(message)s",
        level=logging.DEBUG if args.verbose else logging.WARNING,
    )

    match args.command:
        case "toggle":
            _cmd_toggle(args)
        case "status":
            _cmd_status()
        case "is-running":
            _cmd_is_running()
        case "kill":
            _cmd_kill()

if __name__ == "__main__":
    main()
