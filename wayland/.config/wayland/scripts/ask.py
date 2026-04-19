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

def _focused_monitor_width_logical() -> Optional[int]:
    """Ask the running compositor for the focused output's logical width
    (pixel width divided by applied scale). Tries Hyprland first, then
    sway. Returns None if neither is available / answers cleanly."""
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
                w = m.get("width")
                scale = m.get("scale") or 1
                if w:
                    return int(w / scale)
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
                mode = o.get("current_mode") or {}
                w = mode.get("width")
                scale = o.get("scale") or 1
                if w:
                    return int(w / scale)
    except (
        FileNotFoundError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
    ):
        pass

    return None

BASE_CSS = """
/* Tokens — keep design decisions centralised via @define-color so the
 * rest of the sheet reads like semantic role applications, not hex
 * noise. Adjust here, ripple everywhere. */
@define-color ask_bg        #1a1c23;
@define-color ask_bg_soft   rgba(255, 255, 255, 0.04);
@define-color ask_bg_softer rgba(255, 255, 255, 0.025);
@define-color ask_border    rgba(255, 255, 255, 0.08);
@define-color ask_border_hi rgba(255, 255, 255, 0.14);
@define-color ask_fg        #e5e9f0;
@define-color ask_fg_dim    #9aa0a6;
@define-color ask_accent    #6ea8fe;
@define-color ask_idle      #8fbf7a;
@define-color ask_stream    #e6c07b;
@define-color ask_pending   #e06c75;

/* Overall surface. Slightly warmer than pure black, with a hint of
 * translucency so compositor blur can peek through when enabled. */
window {
    background: rgba(26, 28, 35, 0.97);
    color: @ask_fg;
}

/* Header bar — compact, a subtle divider. No background colour of its
 * own so the provider pill reads as the one emphasized element. */
box.ask-header {
    padding: 10px 14px 10px 16px;
    border-bottom: 1px solid @ask_border;
}

/* Provider pill: rounded background, coloured text + tinted fill per
 * phase. The colour contract is the same as the waybar module — green
 * when idle, yellow when streaming, red when pending first chunk. */
label.ask-provider {
    font-size: 11pt;
    font-weight: 600;
    padding: 4px 10px;
    margin-right: 6px;
    border-radius: 999px;
    background: @ask_bg_soft;
    color: @ask_fg;
}
label.ask-provider.idle {
    color: @ask_idle;
    background: alpha(@ask_idle, 0.14);
}
label.ask-provider.streaming {
    color: @ask_stream;
    background: alpha(@ask_stream, 0.14);
}
label.ask-provider.pending {
    color: @ask_pending;
    background: alpha(@ask_pending, 0.14);
}

/* Header close button. Minimal, goes red on hover so there's a clear
 * "this ends things" hint without being shouty at rest. */
button.ask-close {
    background: transparent;
    border: none;
    padding: 4px 10px;
    min-width: 0;
    color: @ask_fg_dim;
    font-size: 12pt;
    border-radius: 6px;
}
button.ask-close:hover {
    color: @ask_pending;
    background: alpha(@ask_pending, 0.12);
}

/* Conversation scroller — textview is transparent so the window colour
 * shows through; content padding via margins on the TextView itself. */
scrolledwindow {
    background: transparent;
}
textview {
    font-size: 14pt;
    background: transparent;
    color: @ask_fg;
}
textview text {
    background: transparent;
    color: @ask_fg;
}

/* Compose — framed box with a focus ring, same rounding as the pill so
 * the surface feels coherent. Box grows up to max_lines thanks to the
 * ScrolledWindow cap set in Python. */
scrolledwindow.ask-compose {
    margin: 10px 12px 12px 12px;
    border: 1px solid @ask_border;
    border-radius: 10px;
    background: @ask_bg_soft;
}
scrolledwindow.ask-compose:focus-within {
    border-color: alpha(@ask_accent, 0.6);
    background: @ask_bg_soft;
    box-shadow: 0 0 0 2px alpha(@ask_accent, 0.18);
}
textview.ask-compose-text,
textview.ask-compose-text text {
    font-family: monospace;
    background: transparent;
    font-size: 12pt;
    color: @ask_fg;
}

/* Queue panel — treated as a stacked card strip above the compose. */
box.ask-queue {
    background: @ask_bg_softer;
    border-top: 1px solid @ask_border;
    padding: 6px 0 2px 0;
}
label.ask-queue-header {
    font-size: 9pt;
    font-weight: 700;
    color: @ask_fg_dim;
    padding: 0 14px 4px 14px;
    letter-spacing: 0.08em;
}

/* Each row is its own card. Listbox gets no background — rows own it. */
list {
    background: transparent;
}
row.ask-queue-row {
    margin: 2px 8px;
    border-radius: 8px;
    background: @ask_bg_soft;
    border: 1px solid @ask_border;
    transition: background 120ms ease, border-color 120ms ease;
}
row.ask-queue-row:hover {
    background: alpha(@ask_accent, 0.08);
    border-color: alpha(@ask_accent, 0.4);
}
label.ask-queue-preview {
    font-size: 11pt;
    color: @ask_fg;
}

/* Row buttons — ghost by default, tint on hover so they don't compete
 * with the preview label for attention until the user reaches for them. */
button.ask-queue-send,
button.ask-queue-remove {
    background: transparent;
    border: none;
    padding: 4px 10px;
    min-width: 0;
    color: @ask_fg_dim;
    border-radius: 6px;
}
button.ask-queue-send:hover {
    color: @ask_idle;
    background: alpha(@ask_idle, 0.14);
}
button.ask-queue-remove:hover {
    color: @ask_pending;
    background: alpha(@ask_pending, 0.14);
}
"""

class MarkdownView:
    """Renders a full text buffer as CommonMark via `markdown-it-py`.

    Streaming callers call `render(full_text)` each time new chunks
    arrive; we re-parse from scratch (cheap for typical AI-response
    sizes). Token walking maintains a stack of active TextTags that is
    pushed/popped on `*_open`/`*_close` tokens. Per-link TextTags carry
    the target URL on a `.url` attribute so the click handler can
    resolve them."""

    HEADING_SCALES = {1: 1.4, 2: 1.2, 3: 1.1}
    LINK_COLOR = "#6ea8fe"
    CODE_BG = "#1e1e1e"
    INLINE_CODE_BG = "#2a2a2a"

    def __init__(self, buffer: Gtk.TextBuffer):
        self.buffer = buffer
        self._tags = self._build_static_tags()
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
        self.buffer.set_text("")
        tokens = self._md.parse(text)
        self._walk(tokens, tag_stack=[], list_stack=[])

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
        if tags:
            self.buffer.insert_with_tags(end, text, *tags)
        else:
            self.buffer.insert(end, text)

class ComposeView:
    """Multi-line compose area with Enter-to-submit, Shift+Enter for a
    newline, auto-growing up to `max_lines` before scrolling. Ctrl+P
    is wired from the window and reaches text in through `append_text`."""

    def __init__(self, max_lines: int = 6, on_submit=None):
        self._on_submit = on_submit
        self.scroller = Gtk.ScrolledWindow()
        self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroller.set_propagate_natural_height(True)
        self.scroller.add_css_class("ask-compose")

        self._textview = Gtk.TextView(
            wrap_mode=Gtk.WrapMode.WORD_CHAR,
            top_margin=8,
            bottom_margin=8,
            left_margin=10,
            right_margin=10,
            accepts_tab=False,
        )
        self._textview.add_css_class("ask-compose-text")
        self.scroller.set_child(self._textview)

        # Cap natural height at ~max_lines. Pango metrics come back in Pango
        # units (PANGO_SCALE == 1024); convert to pixels for GTK size props.
        metrics = self._textview.get_pango_context().get_metrics(None)
        line_px = (metrics.get_ascent() + metrics.get_descent()) / Pango.SCALE
        pad = 16
        self.scroller.set_max_content_height(int(line_px * max_lines) + pad)
        self.scroller.set_min_content_height(int(line_px) + pad)

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

    def _on_key(self, _controller, keyval, _keycode, state) -> bool:
        if keyval not in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            return False
        if state & Gdk.ModifierType.SHIFT_MASK:
            # Shift+Enter: default handler inserts a newline, grow the box.
            return False
        text = self.get_text().strip()
        if text and self._on_submit:
            self.clear()
            self._on_submit(text)

        return True

class QueueRow(Gtk.ListBoxRow):
    """A pending turn waiting for its slot. Preview label + send (⏎) +
    remove (✕) buttons. Double-click the preview to edit in place — an
    `Entry` replaces the label, Enter commits, focus-out also commits."""

    def __init__(self, text: str, on_send, on_remove, on_edit_commit):
        super().__init__()
        self._text = text
        self._on_send = on_send
        self._on_remove = on_remove
        self._on_edit_commit = on_edit_commit
        self._editing = False
        self._entry: Optional[Gtk.Entry] = None
        self.add_css_class("ask-queue-row")

        self._row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._row.set_margin_top(4)
        self._row.set_margin_bottom(4)
        self._row.set_margin_start(8)
        self._row.set_margin_end(8)

        self._label = Gtk.Label(label=self._preview(text), xalign=0.0, hexpand=True)
        self._label.set_ellipsize(Pango.EllipsizeMode.END)
        self._label.add_css_class("ask-queue-preview")
        self._row.append(self._label)

        send_btn = Gtk.Button(label="⏎")
        send_btn.add_css_class("ask-queue-send")
        send_btn.set_tooltip_text("Send this now")
        send_btn.connect("clicked", lambda _b: self._on_send(self))
        self._row.append(send_btn)

        remove_btn = Gtk.Button(label="✕")
        remove_btn.add_css_class("ask-queue-remove")
        remove_btn.set_tooltip_text("Drop from queue")
        remove_btn.connect("clicked", lambda _b: self._on_remove(self))
        self._row.append(remove_btn)

        self.set_child(self._row)

        click = Gtk.GestureClick()
        click.set_button(Gdk.BUTTON_PRIMARY)
        click.connect("pressed", self._on_click)
        self.add_controller(click)

    def text(self) -> str:
        return self._text

    @staticmethod
    def _preview(text: str) -> str:
        compact = " ".join(text.split())
        if len(compact) > 140:
            compact = compact[:137] + "…"

        return compact

    def _on_click(self, _gesture, n_press, _x, _y) -> None:
        if n_press == 2 and not self._editing:
            self._enter_edit_mode()

    def _enter_edit_mode(self) -> None:
        self._editing = True
        self._row.remove(self._label)
        entry = Gtk.Entry(hexpand=True)
        entry.set_text(self._text)
        entry.connect("activate", lambda _e: self._commit_edit())
        focus = Gtk.EventControllerFocus()
        focus.connect("leave", lambda _f: self._commit_edit())
        entry.add_controller(focus)
        self._row.prepend(entry)
        entry.grab_focus()
        entry.set_position(-1)
        self._entry = entry

    def _commit_edit(self) -> None:
        if not self._editing or self._entry is None:
            return
        new_text = self._entry.get_text().strip()
        self._editing = False
        self._row.remove(self._entry)
        self._entry = None
        if new_text:
            self._text = new_text
        self._label.set_label(self._preview(self._text))
        self._row.prepend(self._label)
        self._on_edit_commit(self, self._text)

class AskWindow(Gtk.ApplicationWindow):
    """Layer-shell sidebar anchored to the right edge, full-height.

    Public API: `dispatch_turn(user_message)` — append a user turn and
    stream the adapter's response into the markdown view. Safe to call
    from the GTK thread; the adapter runs in a worker. Turns submitted
    while a stream is in flight are pushed onto a visible queue and
    drained one at a time as each stream finishes."""

    def __init__(self, app: Gtk.Application, adapter: ConversationAdapter):
        super().__init__(application=app, title="Ask")
        self._adapter = adapter
        self._text = ""
        self._streaming = False
        self._alive = True
        self._queue: list[QueueRow] = []
        # idle: ready for a turn; pending: user turn sent, no chunk received
        # yet; streaming: first chunk arrived, adapter still yielding. Both
        # the overlay header and the waybar module read this to pick colour.
        self._phase: str = "idle"
        self._install_css()

        Gtk4LayerShell.init_for_window(self)
        # Explicit namespace so compositors can target us with a stable
        # layerrule (`layer = namespace:^(ask)$`) regardless of the
        # application_id GDK would otherwise advertise as the namespace.
        Gtk4LayerShell.set_namespace(self, "ask")
        Gtk4LayerShell.set_layer(self, Gtk4LayerShell.Layer.TOP)
        Gtk4LayerShell.set_anchor(self, Gtk4LayerShell.Edge.TOP, True)
        Gtk4LayerShell.set_anchor(self, Gtk4LayerShell.Edge.BOTTOM, True)
        Gtk4LayerShell.set_anchor(self, Gtk4LayerShell.Edge.RIGHT, True)
        # EXCLUSIVE so `toggle-window` / `present()` actually land keyboard
        # focus on the compose entry the moment we map. ON_DEMAND requires a
        # click before the surface is allowed to receive keys, which breaks
        # the "Shift+a and start typing" flow. Escape hides → grab releases.
        Gtk4LayerShell.set_keyboard_mode(self, Gtk4LayerShell.KeyboardMode.EXCLUSIVE)
        self.set_default_size(self._overlay_width(), -1)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.add_css_class("ask-root")

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        header.add_css_class("ask-header")
        self._provider_label = Gtk.Label(
            label=f"󱍊 {adapter.provider.value}",
            xalign=0.0,
            hexpand=True,
        )
        self._provider_label.add_css_class("ask-provider")
        self._provider_label.add_css_class("idle")
        close_button = Gtk.Button(label="✕")
        close_button.add_css_class("ask-close")
        close_button.connect("clicked", lambda _b: self.close())
        header.append(self._provider_label)
        header.append(close_button)
        root.append(header)

        self._scroller = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        self._textview = Gtk.TextView(
            editable=False,
            cursor_visible=False,
            wrap_mode=Gtk.WrapMode.WORD_CHAR,
            left_margin=14,
            right_margin=14,
            top_margin=14,
            bottom_margin=14,
        )
        self._scroller.set_child(self._textview)
        root.append(self._scroller)

        # Queue panel — hidden until a turn is enqueued.
        self._queue_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._queue_box.add_css_class("ask-queue")
        queue_header = Gtk.Label(label="queued", xalign=0.0)
        queue_header.add_css_class("ask-queue-header")
        queue_header.set_margin_top(4)
        queue_header.set_margin_bottom(2)
        queue_header.set_margin_start(10)
        self._queue_box.append(queue_header)
        self._queue_listbox = Gtk.ListBox()
        self._queue_listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self._queue_box.append(self._queue_listbox)
        self._queue_box.set_visible(False)
        root.append(self._queue_box)

        self._compose = ComposeView(max_lines=6, on_submit=self.dispatch_turn)
        root.append(self._compose.scroller)

        self.set_child(root)

        self._md = MarkdownView(self._textview.get_buffer())

        self._wire_link_clicks()
        self._wire_keys()
        self.connect("close-request", self._on_close_request)

    @staticmethod
    def _overlay_width(fraction: float = 0.4) -> int:
        """Fraction of the *current* (focused) monitor's logical width.
        Asks the compositor directly — Hyprland first, then sway — so the
        overlay sizes to wherever the user is looking, not whatever GDK
        happens to list as the default monitor. Falls back to a reasonable
        default when nobody answers."""
        width = _focused_monitor_width_logical()
        if width is None:
            # Last resort: GDK's first-monitor geometry. Better than hard-
            # coding 520 if we have any display info at all.
            display = Gdk.Display.get_default()
            if display is not None:
                monitors = display.get_monitors()
                if monitors.get_n_items() > 0:
                    width = monitors.get_item(0).get_geometry().width
        if not width:
            return 520

        return max(320, int(width * fraction))

    def append(self, chunk: str) -> None:
        pin_to_bottom = self._at_bottom()
        self._text += chunk
        self._md.render(self._text)
        if pin_to_bottom:
            GLib.idle_add(self._scroll_to_end)

    def focus_compose(self) -> None:
        self._compose.focus()

    def toggle_visibility(self) -> bool:
        """Flip overlay visibility without tearing down the session. Safe to
        call from the GTK main thread; returns False so it can be passed
        straight to `GLib.idle_add`."""
        if self.get_visible():
            self.set_visible(False)
        else:
            self.set_visible(True)
            self.present()
            self._compose.focus()

        return False

    def dispatch_turn(self, user_message: str) -> None:
        message = user_message.strip()
        if not message:
            return
        if not self.get_visible():
            # Escape hid the overlay; a new turn brings it back. present()
            # (after set_visible) re-grabs focus and raises.
            self.set_visible(True)
            self.present()
        if self._streaming:
            # Previous turn still in flight — queue this one. It'll drain
            # automatically when `_mark_idle` fires, and the send/edit
            # controls let the user reorder or skip ahead manually.
            self._enqueue(message)
            return
        self._start_turn(message)

    def _start_turn(self, message: str) -> None:
        self._append_user_turn(message)
        self._streaming = True
        self._compose.set_sensitive(False)
        self._set_phase("pending")
        threading.Thread(target=self._run_turn, args=(message,), daemon=True).start()

    def _set_phase(self, phase: str) -> bool:
        """Central place to mutate phase state, swap the provider-label CSS
        class, and poke waybar. Returns False so it composes with idle_add."""
        self._phase = phase
        for cls in ("idle", "pending", "streaming"):
            if cls == phase:
                self._provider_label.add_css_class(cls)
            else:
                self._provider_label.remove_css_class(cls)
        _signal_waybar_safe()

        return False

    def phase(self) -> str:
        return self._phase

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
        text = self._remove_queue_row(row)
        if not text:
            return
        if self._streaming:
            # Promote to head of the queue rather than stepping on the
            # in-flight stream. `_mark_idle` will drain it next.
            self._queue.insert(
                0,
                QueueRow(
                    text=text,
                    on_send=self._on_queue_send,
                    on_remove=self._on_queue_remove,
                    on_edit_commit=self._on_queue_edit,
                ),
            )
            self._queue_listbox.prepend(self._queue[0])
            self._queue_box.set_visible(True)
            return
        self._start_turn(text)

    def _on_queue_remove(self, row: QueueRow) -> None:
        self._remove_queue_row(row)

    def _on_queue_edit(self, row: QueueRow, new_text: str) -> None:
        # Row keeps its slot; the user-visible label already shows the new
        # preview. Nothing else to do here — dispatch_turn will read
        # row.text() when the queue drains or the send button is hit.
        pass

    def _append_user_turn(self, user_message: str) -> None:
        prefix = "\n\n---\n\n" if self._text else ""
        block = f"{prefix}### You:\n\n{user_message}\n\n### Assistant:\n\n"
        self.append(block)

    def _run_turn(self, user_message: str) -> None:
        first_chunk = True
        try:
            for chunk in self._adapter.turn(user_message):
                if not self._alive:
                    return
                if first_chunk:
                    # First delta has arrived — leave the red `pending` state
                    # and go yellow `streaming`. Scheduled before the append
                    # so the colour flip and the first assistant text render
                    # in the same UI tick.
                    GLib.idle_add(self._set_phase, "streaming")
                    first_chunk = False
                GLib.idle_add(self.append, chunk)
        except Exception as e:
            if not self._alive:
                # Window was closed mid-stream; adapter.close() terminated
                # the backend and the resulting error is expected. Swallow.
                return
            log.error("turn failed: %s", e)
            GLib.idle_add(self.append, f"\n\n*error: {e}*\n")
        finally:
            if self._alive:
                GLib.idle_add(self._mark_idle)

    def _mark_idle(self) -> bool:
        self._streaming = False
        if self._alive:
            self._compose.set_sensitive(True)
            self._compose.focus()
            self._set_phase("idle")
        # Drain the next queued turn, if any. Schedule via idle_add so the
        # UI rerenders the current assistant block before the next `You:`
        # block arrives — keeps the streaming transition visible.
        if self._alive:
            nxt = self._pop_queue_front()
            if nxt:
                GLib.idle_add(self._start_turn, nxt)

        return False

    def is_streaming(self) -> bool:
        return self._streaming

    def queue_size(self) -> int:
        return len(self._queue)

    def _at_bottom(self) -> bool:
        adj = self._scroller.get_vadjustment()

        return adj.get_value() + adj.get_page_size() >= adj.get_upper() - 10

    def _scroll_to_end(self) -> bool:
        end_iter = self._textview.get_buffer().get_end_iter()
        self._textview.scroll_to_iter(end_iter, 0.0, False, 0.0, 0.0)

        return False

    def _wire_link_clicks(self) -> None:
        click = Gtk.GestureClick()
        click.set_button(Gdk.BUTTON_PRIMARY)
        click.connect("released", self._on_click)
        self._textview.add_controller(click)

    def _on_click(self, _gesture, _n_press, x, y) -> None:
        tv = self._textview
        bx, by = tv.window_to_buffer_coords(Gtk.TextWindowType.WIDGET, int(x), int(y))
        found, iter_ = tv.get_iter_at_location(bx, by)
        if not found:
            return
        for tag in iter_.get_tags():
            url = getattr(tag, "url", None)
            if url:
                Gio.AppInfo.launch_default_for_uri(url, None)
                return

    def _wire_keys(self) -> None:
        key = Gtk.EventControllerKey()
        key.connect("key-pressed", self._on_key)
        self.add_controller(key)

    @staticmethod
    def _install_css() -> None:
        provider = Gtk.CssProvider()
        css = BASE_CSS.encode("utf-8")
        provider.load_from_data(css, len(css))
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

    def _on_key(self, _controller, keyval, _keycode, state) -> bool:
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        if ctrl and keyval == Gdk.KEY_q:
            self.close()
            return True
        if ctrl and keyval == Gdk.KEY_p:
            # Ctrl+P: explicit clipboard paste into the compose entry so
            # the user can slurp the current selection without having to
            # click the field first.
            self._paste_clipboard_into_compose()
            return True
        if keyval == Gdk.KEY_Escape:
            # Hide-not-close: session + socket stay alive so the next
            # forwarder invocation (or a new speech turn) re-shows the
            # overlay and continues the conversation. Ctrl+Q / header ✕
            # remain the hard-close path.
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

def _socket_is_live() -> bool:
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
            if _socket_is_live():
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
            conn.sendall(json.dumps(response).encode())
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
            text = InputAdapterStdin().read()
        case InputMode.CLIPBOARD:
            text = InputAdapterClipboard().read()
        case _:
            raise ValueError(f"unknown input mode: {mode!r}")

    return (text or "").strip()

def _cmd_toggle(args) -> None:
    """Read input (stdin/clipboard) and either forward it to a live session
    or become the session owner and open the overlay."""
    initial = _read_input(args.input)

    # Forwarder path: if a session already owns the socket, ship the
    # initial text as a turn and exit. Empty input (common for press-2 of
    # a speech toggle pair) exits silently without disturbing the session.
    status = _send("status")
    if status and status.get("ok"):
        if initial:
            _send("turn", text=initial)

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
    sys.exit(0 if _socket_is_live() else 1)

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

def _cmd_toggle_window() -> None:
    """Flip overlay visibility on the running session. No-op (silent) when
    no session is alive — the keybind can't do anything useful there."""
    _send("toggle-window")

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
        choices=["http", "claude", "codex"],
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
        default=[],
        metavar="KEY",
        choices=[
            "web_search",
            "code_interpreter",
            "image_generation",
            "memory",
            "voice",
        ],
        help="enable a built-in feature (repeatable). OpenWebUI-only.",
    )

    subparsers.add_parser("status", help="print waybar-shaped JSON status")
    subparsers.add_parser(
        "is-running",
        help="exit 0 if a session is live, non-zero otherwise",
    )
    subparsers.add_parser("kill", help="terminate the running session (if any)")
    subparsers.add_parser(
        "toggle-window",
        help="show or hide the overlay without ending the session",
    )

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
        case "toggle-window":
            _cmd_toggle_window()

if __name__ == "__main__":
    main()
