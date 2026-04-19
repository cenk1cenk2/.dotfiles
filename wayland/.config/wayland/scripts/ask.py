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
    DEFAULT_CONVERSE_MODEL,
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

class MarkdownView:
    """Renders a full text buffer as CommonMark via `markdown-it-py`.

    Streaming callers call `render(full_text)` each time new chunks
    arrive; we re-parse from scratch (cheap for typical AI-response
    sizes). Token walking maintains a stack of active TextTags that is
    pushed/popped on `*_open`/`*_close` tokens. Per-link TextTags carry
    the target URL on a `.url` attribute so the click handler can
    resolve them."""

    HEADING_SCALES = {1: 1.4, 2: 1.2, 3: 1.1}
    LINK_COLOR = "#4676ac"
    CODE_BG = "#17191e"
    INLINE_CODE_BG = "#2c333d"
    # Stamped on every inserted run so text is legible regardless of
    # what the active GTK theme does to `textview text { color: … }`.
    # CSS alone can lose to theme `.background` cascades depending on
    # priority; a TextTag with an explicit foreground is definitive.
    FG_COLOR = "#abb2bf"

    def __init__(self, buffer: Gtk.TextBuffer):
        self.buffer = buffer
        self._tags = self._build_static_tags()
        self._fg_tag = buffer.create_tag("ask-fg", foreground=self.FG_COLOR)
        self._md = MarkdownIt("commonmark")

    def _build_static_tags(self) -> dict:
        b = self.buffer

        return {
            "h1": b.create_tag(
                "h1",
                weight=Pango.Weight.BOLD,
                scale=self.HEADING_SCALES[1],
                pixels_above_lines=8,
                pixels_below_lines=4,
            ),
            "h2": b.create_tag(
                "h2",
                weight=Pango.Weight.BOLD,
                scale=self.HEADING_SCALES[2],
                pixels_above_lines=6,
                pixels_below_lines=3,
            ),
            "h3": b.create_tag(
                "h3",
                weight=Pango.Weight.BOLD,
                scale=self.HEADING_SCALES[3],
                pixels_above_lines=4,
                pixels_below_lines=2,
            ),
            "bold": b.create_tag("bold", weight=Pango.Weight.BOLD),
            "italic": b.create_tag("italic", style=Pango.Style.ITALIC),
            "code": b.create_tag(
                "code",
                family="monospace",
                background=self.INLINE_CODE_BG,
            ),
            "code_block": b.create_tag(
                "code_block",
                family="monospace",
                paragraph_background=self.CODE_BG,
                left_margin=16,
                right_margin=16,
                pixels_above_lines=4,
                pixels_below_lines=4,
            ),
            "blockquote": b.create_tag(
                "blockquote",
                style=Pango.Style.ITALIC,
                left_margin=16,
            ),
        }

    def render(self, text: str) -> None:
        # Batch the clear + walk inside a single user-action so GTK
        # coalesces signals and only fires the layout update at the end.
        # Without this, the intermediate empty-buffer state can collapse
        # the TextView's allocation and never re-expand as content arrives.
        buf = self.buffer
        buf.begin_user_action()
        try:
            buf.set_text("")
            tokens = self._md.parse(text)
            self._walk(tokens, tag_stack=[], list_stack=[])
            self._trim_trailing_whitespace()
        finally:
            buf.end_user_action()

    def _trim_trailing_whitespace(self) -> None:
        """Paragraph/heading/list closers emit trailing newlines so blocks
        are separated mid-buffer. The very last closer leaves a couple of
        orphan newlines at the end — invisible in a single-buffer view but
        read as empty lines of padding inside a per-turn card. Walk back
        and delete them so the card hugs its content."""
        buf = self.buffer
        end = buf.get_end_iter()
        start = end.copy()
        while start.backward_char():
            if start.get_char() not in (" ", "\n", "\t"):
                start.forward_char()
                break
        if not start.equal(end):
            buf.delete(start, end)

    def _walk(self, tokens, tag_stack, list_stack) -> None:
        for tok in tokens:
            self._handle(tok, tag_stack, list_stack)

    def _handle(self, tok, tag_stack, list_stack) -> None:  # noqa: C901
        tags = self._tags
        match tok.type:
            case "heading_open":
                tag_stack.append(tags[tok.tag])
            case "heading_close":
                tag_stack.pop()
                self._insert("\n", tag_stack)
            case "paragraph_open":
                pass
            case "paragraph_close":
                # Tight lists reuse paragraphs for item bodies — emit just
                # a single break so we don't double-space list items.
                self._insert("\n" if list_stack else "\n\n", tag_stack)
            case "inline":
                self._walk(tok.children or [], tag_stack, list_stack)
            case "text":
                self._insert(tok.content, tag_stack)
            case "softbreak":
                self._insert(" ", tag_stack)
            case "hardbreak":
                self._insert("\n", tag_stack)
            case "strong_open":
                tag_stack.append(tags["bold"])
            case "strong_close":
                tag_stack.pop()
            case "em_open":
                tag_stack.append(tags["italic"])
            case "em_close":
                tag_stack.pop()
            case "code_inline":
                self._insert(tok.content, tag_stack + [tags["code"]])
            case "link_open":
                href = tok.attrGet("href") or ""
                link_tag = self.buffer.create_tag(
                    None,
                    foreground=self.LINK_COLOR,
                    underline=Pango.Underline.SINGLE,
                )
                link_tag.url = href
                tag_stack.append(link_tag)
            case "link_close":
                tag_stack.pop()
            case "fence" | "code_block":
                content = (
                    tok.content if tok.content.endswith("\n") else tok.content + "\n"
                )
                self._insert(content, [tags["code_block"]])
                self._insert("\n", tag_stack)
            case "bullet_list_open":
                list_stack.append(["bullet", 0])
            case "ordered_list_open":
                list_stack.append(["ordered", int(tok.attrGet("start") or 1)])
            case "bullet_list_close" | "ordered_list_close":
                list_stack.pop()
                if not list_stack:
                    self._insert("\n", tag_stack)
            case "list_item_open":
                ctx = list_stack[-1] if list_stack else None
                indent = "  " * max(0, len(list_stack) - 1)
                if ctx and ctx[0] == "ordered":
                    self._insert(f"{indent}{ctx[1]}. ", tag_stack)
                    ctx[1] += 1
                else:
                    self._insert(f"{indent}• ", tag_stack)
            case "list_item_close":
                pass
            case "blockquote_open":
                tag_stack.append(tags["blockquote"])
            case "blockquote_close":
                tag_stack.pop()
            case "hr":
                self._insert("─" * 40 + "\n\n", tag_stack)
            case _:
                log.debug("unhandled token: %s", tok.type)

    def _insert(self, text: str, tags: list) -> None:
        end = self.buffer.get_end_iter()
        # Always carry the foreground tag so text renders in our palette
        # regardless of theme. Other tags (bold, italic, code, link) stack
        # on top without needing to repeat the colour.
        self.buffer.insert_with_tags(end, text, self._fg_tag, *tags)

class ComposeView:
    """Multi-line compose with an obvious visual box, hint line, and a
    clickable SEND button. Enter submits, Shift+Enter inserts a newline,
    Ctrl+P paste is wired from the window via `append_text`. Auto-grows
    up to `max_lines` before it starts scrolling."""

    def __init__(self, max_lines: int = 6, on_submit=None):
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

        # Cap natural height at ~max_lines. Pango metrics come back in Pango
        # units (PANGO_SCALE == 1024); convert to pixels for GTK size props.
        metrics = self._textview.get_pango_context().get_metrics(None)
        line_px = (metrics.get_ascent() + metrics.get_descent()) / Pango.SCALE
        pad = 22
        self._scroller.set_max_content_height(int(line_px * max_lines) + pad)
        self._scroller.set_min_content_height(int(line_px * 2) + pad)

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar.add_css_class("ask-compose-bar")
        hint = Gtk.Label(
            label="Enter to send  ·  Shift+Enter newline  ·  Ctrl+P paste",
            xalign=0.0,
            hexpand=True,
        )
        hint.add_css_class("ask-compose-hint")
        bar.append(hint)

        self._send_btn = Gtk.Button(label="SEND")
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
    User cards get populated once via `set_text`; assistant cards are
    streamed in chunk-by-chunk via `append`. Each card owns its own
    `Gtk.TextBuffer` + `MarkdownView`, so per-link tag trees are scoped
    to the card that rendered them."""

    def __init__(self, role: str, title: str, on_link):
        self.role = role
        self.widget = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        self.widget.add_css_class("ask-card")
        self.widget.add_css_class(f"ask-card-{role}")

        role_label = Gtk.Label(label=title, xalign=0.0)
        role_label.add_css_class("ask-card-role")
        role_label.add_css_class(f"ask-card-role-{role}")
        self.widget.append(role_label)

        self._textview = Gtk.TextView(
            editable=False,
            cursor_visible=False,
            wrap_mode=Gtk.WrapMode.WORD_CHAR,
            top_margin=2,
            bottom_margin=2,
            left_margin=0,
            right_margin=0,
            hexpand=True,
        )
        self._textview.add_css_class("ask-card-text")
        self.widget.append(self._textview)

        self._md = MarkdownView(self._textview.get_buffer())
        self._text = ""
        self._on_link = on_link

        click = Gtk.GestureClick()
        click.set_button(Gdk.BUTTON_PRIMARY)
        click.connect("released", self._on_click)
        self._textview.add_controller(click)

    def append(self, chunk: str) -> None:
        self._text += chunk
        self._md.render(self._text)
        # Nudge the allocator — TextView's content-height changed, so the
        # parent Box / scroller need to re-measure. Without this, cards
        # can stay at their initial (tiny) allocation and later chunks
        # render into an already-exhausted size.
        self._textview.queue_resize()

    def set_text(self, text: str) -> None:
        self._text = text
        self._md.render(text)
        self._textview.queue_resize()

    def get_text(self) -> str:
        return self._text

    def _on_click(self, _gesture, _n_press, x, y) -> None:
        tv = self._textview
        bx, by = tv.window_to_buffer_coords(Gtk.TextWindowType.WIDGET, int(x), int(y))
        found, iter_ = tv.get_iter_at_location(bx, by)
        if not found:
            return
        for tag in iter_.get_tags():
            url = getattr(tag, "url", None)
            if url:
                self._on_link(url)
                return

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
        self._conv_scroller.set_policy(
            Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC
        )
        self._conv_scroller.add_css_class("ask-conv-scroller")
        self._conv_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        self._conv_box.add_css_class("ask-conv")
        self._conv_scroller.set_child(self._conv_box)
        root.append(self._conv_scroller)

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
        self._compose = ComposeView(max_lines=6, on_submit=self.dispatch_turn)
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
        # Parent the widget BEFORE setting its content so GTK's allocate
        # pass catches the child in the tree on the first layout cycle.
        # Otherwise the first card sometimes doesn't render until a
        # later turn forces a re-layout of the conversation box.
        self._conv_box.append(card.widget)
        card.set_text(text)
        self._conv_box.queue_resize()
        GLib.idle_add(self._scroll_to_end)

        return card

    def _append_assistant_card(self) -> TurnCard:
        card = TurnCard(
            role="assistant",
            title=self._assistant_title(),
            on_link=self._open_link,
        )
        self._cards.append(card)
        self._conv_box.append(card.widget)
        self._conv_box.queue_resize()
        GLib.idle_add(self._scroll_to_end)

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
        """Main-thread-safe sink for adapter chunks. Appends to whichever
        assistant card is currently streaming. Returns False so it
        composes with `GLib.idle_add`."""
        if self._active_assistant is not None:
            pin = self._at_bottom()
            self._active_assistant.append(chunk)
            if pin:
                GLib.idle_add(self._scroll_to_end)

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

    def _at_bottom(self) -> bool:
        adj = self._conv_scroller.get_vadjustment()

        return adj.get_value() + adj.get_page_size() >= adj.get_upper() - 20

    def _scroll_to_end(self) -> bool:
        adj = self._conv_scroller.get_vadjustment()
        adj.set_value(max(0, adj.get_upper() - adj.get_page_size()))

        return False

    def _open_link(self, url: str) -> None:
        Gio.AppInfo.launch_default_for_uri(url, None)

    def _wire_keys(self) -> None:
        key = Gtk.EventControllerKey()
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
        if keyval == Gdk.KEY_Escape:
            self.set_visible(False)
            return True

        return False

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
        focused and resize width to its 40% fraction. Invoked on every
        toggle-to-visible, so switching monitors between uses rehomes
        the overlay to the new one."""
        monitor = _focused_gdk_monitor()
        if monitor is not None:
            Gtk4LayerShell.set_monitor(self, monitor)
        width = self._overlay_width(monitor=monitor)
        # `set_default_size` only takes effect on first show; after that,
        # a fresh width request is what actually resizes the surface.
        self.set_size_request(width, -1)

    @staticmethod
    def _install_css() -> None:
        """Load `ask.css` from alongside this script and register it as an
        APPLICATION-priority style provider (beats theme, loses to user).
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
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
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
    provider = ConversationProvider(args.converse_provider)
    match provider:
        case ConversationProvider.HTTP:
            features = {k: True for k in args.features} if args.features else None

            return ConversationAdapterHttp(
                system_prompt=AI_SYSTEM_PROMPT,
                base_url=args.converse_base_url,
                model=args.converse_model,
                api_key=os.environ.get("AI_KILIC_DEV_API_KEY", ""),
                temperature=args.converse_temperature,
                top_p=args.converse_top_p,
                thinking=args.converse_thinking,
                num_ctx=args.converse_num_ctx,
                tool_ids=args.tool_ids or None,
                features=features,
                user_agent="ask/1.0",
            )
        case ConversationProvider.CLAUDE:
            return ConversationAdapterClaude(AI_SYSTEM_PROMPT)
        case ConversationProvider.CODEX:
            return ConversationAdapterCodex(AI_SYSTEM_PROMPT)
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
    toggle_parser.add_argument("--converse-model", default=DEFAULT_CONVERSE_MODEL)
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
    # providers and by plain OpenAI endpoints.
    toggle_parser.add_argument(
        "--tool-id",
        action="append",
        dest="tool_ids",
        default=[],
        metavar="ID",
        help=(
            "server-side tool UUID (repeatable); use 'server:mcp:<id>' for "
            "an MCP server. OpenWebUI-only."
        ),
    )
    toggle_parser.add_argument(
        "--feature",
        action="append",
        dest="features",
        default=["web_search", "memory"],
        metavar="KEY",
        help="enable a built-in feature (repeatable). OpenWebUI-only.",
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
