#!/usr/bin/env -S sh -c 'exec uv run --project "$(dirname "$0")" "$0" "$@"'
"""pilot — GTK4 layer-shell sidebar that streams a conversational AI response.

Right-side full-height overlay with a markdown scroller and a compose entry
at the bottom. Reads initial text from stdin or clipboard, sends it as the
first user turn, and streams chunks back via a `ConversationAdapter`. A
Unix-socket session lets subsequent invocations forward follow-up turns
into the live window instead of opening a new one."""

from __future__ import annotations
from lib.acp_adapter import AcpAdapter

import errno
import json
import logging
import os
import re
import socket
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

import click
from acp.schema import McpServerStdio

from lib import (
    create_logger,
    DEFAULT_CONVERSE_ADAPTER,
    DEFAULT_SERVER_NAMES,
    ConversationAdapter,
    ConversationAdapterClaude,
    ConversationAdapterOpenCode,
    ConversationProvider,
    InputAdapterClipboard,
    InputAdapterStdin,
    InputMode,
    CodeBlock,
    MarkdownBlock,
    MarkdownMarkup,
    OutputAdapterClipboard,
    PermissionState,
    PlanChunk,
    SessionInfoChunk,
    PromptAttachment,
    ThinkingChunk,
    ToolCall,
    ToolFormatters,
    UserMessageChunk,
    background_tasks,
    get_permission_seeds,
    get_server as _DEFAULT_SERVER_GET,
    load_prompt,
    notify,
    signal_waybar,
)
from lib.skills import (
    list_references_via_mcp,
    list_skills_via_mcp,
    load_skill_references,
    load_skills,
    parse_skill,
    read_reference,
)

# gtk4-layer-shell must be LD_PRELOAD'd at program start: its libwayland
# shim hooks in at load time, so without it `is_supported()` returns false
# and every layer-shell call becomes a no-op — the window falls through
# to a normal xdg_toplevel. Only for `toggle`; waybar-poll commands
# (status / is-running / kill) don't open a window and don't need the
# preload or the GTK display.
#
# IMPORTANT: import from `lib.layer_shell`, NOT `lib.overlay`. The
# overlay module runs `import gi` at module-top, and if gi is imported
# BEFORE the re-exec sets LD_PRELOAD, the layer-shell shim never hooks
# libwayland and the window renders as a normal xdg_toplevel. The
# `layer_shell` sub-module is stdlib-only so importing it has no side
# effects on gi.
from lib.layer_shell import ensure_layer_shell_preload  # noqa: E402

# Only `toggle` opens a GTK window, so the LD_PRELOAD re-exec is
# scoped to that subcommand. We can't use argparse this early (GTK
# imports below haven't run yet), and global flags like
# `--session <name>` push `toggle` past argv[1] — so scan the whole
# argv for the subcommand token. None of the four subcommand names
# (toggle / status / is-running / kill) collide with any value a user
# might pass to `--session` or `-v`, so membership is enough.
if "toggle" in sys.argv[1:]:
    ensure_layer_shell_preload(__file__)

# `lib.overlay` handles the `gi.require_version` calls + layer-shell
# setup; importing it first ensures the right GI versions are pinned
# before we reach for `gi.repository.*` directly below. Pilot-specific
# widgets (TurnCard, PermissionRow, etc.) still need Gio/GLib/Gtk/Gdk
# and Pango, so we pull them from gi.repository after overlay has
# done its version dance.
from lib.overlay import (  # noqa: E402
    CommandPalette,
    LayerOverlayWindow,
    PillVariant,
    load_overlay_css,
    load_css_from_path,
    make_pill,
)
from gi.repository import (  # noqa: E402
    Gdk,  # ty: ignore[unresolved-import]
    Gio,  # ty: ignore[unresolved-import]
    GLib,  # ty: ignore[unresolved-import]
    Gtk,  # ty: ignore[unresolved-import]
    Pango,  # ty: ignore[unresolved-import]
)

log = logging.getLogger("pilot")

@dataclass(frozen=True)
class PilotPaths:
    """Per-session filesystem coordinates. `--session <suffix>` derives a
    parallel set of paths so multiple pilot overlays can coexist on the
    same user."""

    app_id: str
    socket_path: str
    suffix: str = ""

    @classmethod
    def from_suffix(cls, suffix: str) -> PilotPaths:
        suffix = suffix or ""
        runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        base_app = "dev.kilic.wayland.pilot"
        if suffix:
            return cls(
                app_id=f"{base_app}.{suffix}",
                socket_path=os.path.join(runtime, f"wayland-pilot-{suffix}.sock"),
                suffix=suffix,
            )
        return cls(
            app_id=base_app,
            socket_path=os.path.join(runtime, "wayland-pilot.sock"),
        )

# Populated by the click group callback once `--session` has been
# parsed. Module-level so `Session.send` / `Session.is_live` / click
# commands on `Pilot` all read the same paths without threading a
# PilotPaths arg through every caller.
_PATHS: PilotPaths = PilotPaths.from_suffix("")

AI_SYSTEM_PROMPT = load_prompt("pilot.md", relative_to=__file__)

def _signal_waybar_safe() -> None:
    """Nudge waybar's `custom/pilot` module to re-read status. Non-fatal —
    waybar-signal.sh silently ignores unknown modules, and we shouldn't
    let waybar being unavailable take down the overlay."""
    try:
        signal_waybar("pilot")
    except Exception as e:
        log.debug("waybar signal failed: %s", e)

class ComposeView:
    """Multi-line compose with an obvious visual box, hint line, and a
    clickable SEND button. Enter submits, Shift+Enter inserts a newline,
    Ctrl+P paste is wired from the window via `append_text`. Auto-grows
    up to `set_max_content_fraction` of the window height before
    scrolling kicks in."""

    # Minimum rows of compose height. Starts at 3 rows so the input
    # feels like a proper compose box, not a single-line entry, and
    # stays usable on short screens where 25% of the height would be
    # even less.
    MIN_ROWS = 8

    def __init__(self, on_submit=None):
        self._on_submit = on_submit

        self.widget = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.widget.add_css_class("pilot-compose-wrap")

        self._scroller = Gtk.ScrolledWindow()
        self._scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._scroller.set_propagate_natural_height(True)
        self._scroller.add_css_class("pilot-compose")

        self._textview = Gtk.TextView(
            wrap_mode=Gtk.WrapMode.WORD_CHAR,
            top_margin=10,
            bottom_margin=10,
            left_margin=12,
            right_margin=12,
            accepts_tab=False,
        )
        self._textview.add_css_class("pilot-compose-text")
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
        self._scroller.set_max_content_height(int(self._line_px * 6) + self._pad_px)

        # Resource-pill strip. Sits ABOVE the hint/send bar so picked
        # skills / references surface visibly before submit. Hidden
        # until the first resource is picked. Caller registers a
        # remove callback via `set_resource_pills`.
        self._resource_flow = Gtk.FlowBox(
            orientation=Gtk.Orientation.HORIZONTAL,
            column_spacing=4,
            row_spacing=4,
            hexpand=True,
            valign=Gtk.Align.START,
        )
        self._resource_flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self._resource_flow.set_max_children_per_line(16)
        self._resource_flow.set_homogeneous(False)
        self._resource_flow.add_css_class("pilot-compose-resources")
        self._resource_flow.set_visible(False)
        self.widget.append(self._resource_flow)
        self._resource_remove_cb: Optional[Callable[[str, str], None]] = None

        # Attachment-pill strip. Shows non-text payloads the user has
        # queued up (pasted images today, audio / arbitrary blobs
        # later). Sits between the resource pills and the hint/send bar
        # so a pasted image is visible BEFORE submit without polluting
        # the compose TextView. Hidden until the first attachment lands.
        self._attachment_flow = Gtk.FlowBox(
            orientation=Gtk.Orientation.HORIZONTAL,
            column_spacing=4,
            row_spacing=4,
            hexpand=True,
            valign=Gtk.Align.START,
        )
        self._attachment_flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self._attachment_flow.set_max_children_per_line(16)
        self._attachment_flow.set_homogeneous(False)
        self._attachment_flow.add_css_class("pilot-compose-attachments")
        self._attachment_flow.set_visible(False)
        self.widget.append(self._attachment_flow)
        self._attachment_remove_cb: Optional[Callable[[object], None]] = None

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar.add_css_class("pilot-compose-bar")

        hint = Gtk.Label(
            label="pilot",
            xalign=0.0,
            hexpand=True,
        )
        hint.add_css_class("pilot-compose-hint")
        self._hint_label = hint
        bar.append(hint)

        self._send_btn = Gtk.Button(label="󰌑 send")
        self._send_btn.add_css_class("pilot-compose-send")
        self._send_btn.set_tooltip_text("Send the current message (Enter)")
        self._send_btn.connect("clicked", lambda _b: self._submit())
        bar.append(self._send_btn)
        self.widget.append(bar)

        key = Gtk.EventControllerKey()
        key.connect("key-pressed", self._on_key)
        self._textview.add_controller(key)

    def set_max_content_fraction(self, window_height_px: int, fraction: float) -> None:
        """Cap the compose scroller at `fraction` of the overlay's total
        height. The overlay is anchored top+bottom so its height equals
        the monitor's geometry height; callers pass that in."""
        cap = int(window_height_px * fraction)
        floor = int(self._line_px * self.MIN_ROWS) + self._pad_px
        self._scroller.set_max_content_height(max(floor, cap))

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

    def stage_text(self, text: str) -> None:
        """Drop `text` into the compose buffer without dispatching. Used
        when a new turn arrives while the overlay is already visible
        (socket-forwarded speech transcript, second invocation, etc.) —
        the user gets a chance to edit + Enter instead of having the
        payload auto-submitted. Existing compose content is preserved
        and the new text is appended on a FRESH LINE so it reads as a
        second paragraph rather than getting glued to whatever the user
        was mid-typing."""
        if not text:
            return
        buf = self._textview.get_buffer()
        existing = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)
        if existing:
            sep = "" if existing.endswith("\n") else "\n"
            buf.insert(buf.get_end_iter(), f"{sep}{text}")
        else:
            buf.set_text(text)
        # Cursor to the end so Enter submits everything that's now
        # in the buffer, not wherever the caret last sat.
        buf.place_cursor(buf.get_end_iter())

    def clear(self) -> None:
        self.set_text("")

    def focus(self) -> None:
        self._textview.grab_focus()

    def set_sensitive(self, sensitive: bool) -> None:
        self._textview.set_sensitive(sensitive)
        self._send_btn.set_sensitive(sensitive)

    def set_resource_pills(
        self,
        entries: list[tuple[str, str, str]],
        on_remove: Optional[Callable[[str, str], None]],
    ) -> None:
        """Re-render the resource-pill strip above the compose bar.
        Each entry is `(kind, name, description)`; `on_remove(kind,
        name)` fires when the pill's 󰅖 is clicked. Empty list hides
        the strip. CodeCompanion-style: picked skills live as chips
        instead of polluting the compose text with `#{}` tokens, and
        `PilotWindow.dispatch_turn` prepends their bodies at submit."""
        self._resource_remove_cb = on_remove
        child = self._resource_flow.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._resource_flow.remove(child)
            child = nxt
        if not entries:
            self._resource_flow.set_visible(False)
            return
        for kind, name, desc in entries:
            btn = Gtk.Button(label=f"{kind}/{name} 󰅖")
            btn.add_css_class("pilot-compose-resource")
            btn.add_css_class(f"resource-kind-{kind}")
            if desc:
                btn.set_tooltip_text(desc)
            btn.connect(
                "clicked",
                lambda _b, k=kind, n=name: self._on_resource_remove(k, n),
            )
            self._resource_flow.append(btn)
        self._resource_flow.set_visible(True)

    def _on_resource_remove(self, kind: str, name: str) -> None:
        if self._resource_remove_cb is not None:
            self._resource_remove_cb(kind, name)

    def set_attachment_pills(
        self,
        entries: list[tuple[str, str, object]],
        on_remove: Optional[Callable[[object], None]],
    ) -> None:
        """Render the attachment-pill strip. Each entry is
        `(label, mime, key)` — the pill shows `label` and, on 󰅖,
        fires `on_remove(key)` so the caller can resolve the
        original `PromptAttachment` without us leaking its type
        here. Empty list hides the strip."""
        self._attachment_remove_cb = on_remove
        child = self._attachment_flow.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._attachment_flow.remove(child)
            child = nxt
        if not entries:
            self._attachment_flow.set_visible(False)
            return
        for label, mime, key in entries:
            btn = Gtk.Button(label=f"{label} 󰅖")
            btn.add_css_class("pilot-compose-attachment")
            mime_kind = (mime or "").split("/", 1)[0] or "blob"
            btn.add_css_class(f"attachment-kind-{mime_kind}")
            if mime:
                btn.set_tooltip_text(mime)
            btn.connect(
                "clicked",
                lambda _b, k=key: self._on_attachment_remove(k),
            )
            self._attachment_flow.append(btn)
        self._attachment_flow.set_visible(True)

    def _on_attachment_remove(self, key: object) -> None:
        if self._attachment_remove_cb is not None:
            self._attachment_remove_cb(key)

    def _submit(self) -> None:
        text = self.get_text().strip()
        if not text:
            return
        if self._on_submit:
            self.clear()
            self._on_submit(text)

    def _on_key(self, _controller, keyval, _keycode, state) -> bool:
        if keyval == Gdk.KEY_Escape:
            # Esc with the compose focused: collapse any active text
            # selection but keep focus on the TextView. GTK's
            # built-in Esc on TextView is a no-op by default, so
            # we implement the "standard editor" behaviour
            # explicitly: if there IS a selection, clear it; if
            # there isn't, let Esc propagate (window handler treats
            # it as fall-through — no-op unless a palette's open).
            buf = self._textview.get_buffer()
            if buf.get_has_selection():
                cursor = buf.get_iter_at_mark(buf.get_insert())
                buf.place_cursor(cursor)
                return True
            return False
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
    an action strip at the bottom: `󰏫 edit` toggles an inline multi-line
    editor, `󰌑 send` promotes the message to the next slot, `󰅖 drop`
    removes it. In edit mode, Ctrl+Enter commits; the edit button
    relabels to `󰄬 save` while editing."""

    def __init__(
        self,
        text: str,
        on_send,
        on_remove,
        on_edit_commit,
        *,
        display: Optional[str] = None,
        attachments: Optional[list[PromptAttachment]] = None,
    ):
        super().__init__()
        # `_text` is the wire prompt (may be huge — inlined skill
        # bodies etc.); `_display` is what lands in the queue label
        # and the edit-mode textview. Defaults to `text` for callers
        # that haven't split the two (compose-less enqueues).
        self._text = text
        self._display = display if display is not None else text
        self._attachments: list[PromptAttachment] = list(attachments or [])
        self._on_send = on_send
        self._on_remove = on_remove
        self._on_edit_commit = on_edit_commit
        self._editing = False
        self._edit_scroller: Optional[Gtk.ScrolledWindow] = None
        self._edit_textview: Optional[Gtk.TextView] = None
        self.add_css_class("pilot-queue-row")

        self._card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._card.add_css_class("pilot-queue-card")

        self._label = Gtk.Label(label=self._display, xalign=0.0, hexpand=True)
        self._label.set_wrap(True)
        self._label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self._label.set_selectable(True)
        self._label.add_css_class("pilot-queue-text")
        self._card.append(self._label)

        actions = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=4,
            halign=Gtk.Align.END,
        )
        actions.add_css_class("pilot-queue-actions")

        self._edit_btn = Gtk.Button(label="󰏫 edit")
        self._edit_btn.add_css_class("pilot-queue-edit")
        self._edit_btn.set_tooltip_text("Edit this message")
        self._edit_btn.connect("clicked", lambda _b: self._toggle_edit())
        actions.append(self._edit_btn)

        send_btn = Gtk.Button(label="󰌑 send")
        send_btn.add_css_class("pilot-queue-send")
        send_btn.set_tooltip_text("Promote and dispatch this message now")
        send_btn.connect("clicked", lambda _b: self._on_send(self))
        actions.append(send_btn)

        remove_btn = Gtk.Button(label="󰅖 drop")
        remove_btn.add_css_class("pilot-queue-remove")
        remove_btn.set_tooltip_text("Remove this message from the queue")
        remove_btn.connect("clicked", lambda _b: self._on_remove(self))
        actions.append(remove_btn)

        self._card.append(actions)
        self.set_child(self._card)

    def text(self) -> str:
        """Wire prompt handed to the adapter (may include inlined
        resource bodies)."""
        return self._text

    def display(self) -> str:
        """Clean prose for the user card when this queued row drains."""
        return self._display

    def attachments(self) -> list[PromptAttachment]:
        """Binary content blocks that ride on this queued turn when it
        drains. Captured at enqueue time so the user can keep pasting
        new attachments into the compose without disturbing rows
        already sitting in the queue."""
        return list(self._attachments)

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
        scroller.add_css_class("pilot-queue-edit")

        textview = Gtk.TextView(
            wrap_mode=Gtk.WrapMode.WORD_CHAR,
            top_margin=6,
            bottom_margin=6,
            left_margin=8,
            right_margin=8,
        )
        textview.add_css_class("pilot-queue-edit-text")
        # Edit the DISPLAY text (clean user prose), not the wire
        # prompt — we don't want to expose 30KB of inlined skill body
        # in the editor. On commit the wire text collapses to the
        # edited display: the resources that were attached when the
        # original submission happened are lost, matching the intuitive
        # "this is now a brand new message" semantic.
        textview.get_buffer().set_text(self._display)
        scroller.set_child(textview)

        key = Gtk.EventControllerKey()
        key.connect("key-pressed", self._on_edit_key)
        textview.add_controller(key)

        self._card.prepend(scroller)
        textview.grab_focus()
        self._edit_scroller = scroller
        self._edit_textview = textview
        self._edit_btn.set_label("󰄬 save")

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
            self._display = new_text
        self._label.set_label(self._display)
        self._card.prepend(self._label)
        self._edit_btn.set_label("󰏫 edit")
        self._on_edit_commit(self, self._text)

def _make_markdown_label(
    markup: str,
    *,
    css_classes: tuple[str, ...] = (),
    on_link=None,
    wrap_mode: Pango.WrapMode = Pango.WrapMode.WORD_CHAR,
    selectable: bool = True,
) -> Gtk.Label:
    """Build a Pango-markup-rendering `Gtk.Label` with the wrap / yalign /
    hexpand flags the rest of the overlay expects. Centralised so the
    widget builder below and the legacy call sites that still use a
    single label share one configuration."""
    label = Gtk.Label(
        xalign=0.0,
        yalign=0.0,
        hexpand=True,
        wrap=True,
        wrap_mode=wrap_mode,
        natural_wrap_mode=Gtk.NaturalWrapMode.WORD,
        use_markup=True,
        selectable=selectable,
    )
    label.set_markup(markup)
    for cls in css_classes:
        label.add_css_class(cls)
    if on_link is not None:
        label.connect("activate-link", lambda _lbl, uri: (on_link(uri), True)[1])
    return label

def _make_code_block_widget(block: CodeBlock) -> Gtk.Box:
    """Wrap a `CodeBlock` from `MarkdownMarkup.render_blocks` into a
    full-width Gtk.Box with:

      - a header strip that carries the language hint as a small pill
        floated on the right (empty string → no header rendered);
      - a `Gtk.TextView` body that renders the token stream with
        per-token `Gtk.TextTag` foregrounds. TextView (not Label) so
        `set_pixels_above_lines` / `set_pixels_below_lines` /
        `set_pixels_inside_wrap` give us real per-line pixel spacing
        — Pango-in-Label clips the top of the first glyph row because
        ascenders on mixed-metric monospace spans overshoot the line
        box computed from the surrounding label's font metrics.

    The box itself owns the `.pilot-code-block` CSS class, which
    paints the gutter. Header + body have their own classes so the
    stylesheet can tune typography independently."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
    box.add_css_class("pilot-code-block")

    lang = (block.language or "").strip()
    if lang:
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, hexpand=True)
        header.add_css_class("pilot-code-block-header")
        # Spacer pushes the pill to the right edge. A plain-empty label
        # with hexpand does the trick without pulling in `Gtk.Separator`
        # which would paint a line we don't want.
        spacer = Gtk.Label(label="", xalign=0.0, hexpand=True)
        header.append(spacer)
        pill = Gtk.Label(label=lang, xalign=1.0)
        pill.add_css_class("pilot-code-block-lang")
        header.append(pill)
        box.append(header)

    view = Gtk.TextView(
        editable=False,
        cursor_visible=False,
        monospace=True,
        wrap_mode=Gtk.WrapMode.WORD_CHAR,
        hexpand=True,
    )
    view.set_pixels_above_lines(2)
    view.set_pixels_below_lines(2)
    view.set_pixels_inside_wrap(2)
    view.set_left_margin(12)
    view.set_right_margin(12)
    view.set_top_margin(8)
    view.set_bottom_margin(8)
    view.add_css_class("pilot-code-block-body")

    buf = view.get_buffer()
    # Cache one TextTag per foreground color so tokens with the same
    # colour reuse the same tag instead of churning the tag table.
    color_tags: dict[str, Gtk.TextTag] = {}

    def _tag_for(color: str) -> Gtk.TextTag:
        tag = color_tags.get(color)
        if tag is None:
            tag = buf.create_tag(None, foreground=color)
            color_tags[color] = tag
        return tag

    for text, color in block.tokens:
        buf.insert_with_tags(buf.get_end_iter(), text, _tag_for(color))

    box.append(view)
    return box

def _rebuild_markdown_body(
    container: Gtk.Box,
    blocks: list[MarkdownBlock],
    *,
    text_css_classes: tuple[str, ...] = (),
    on_link=None,
) -> None:
    """Rebuild `container` as one widget per `MarkdownBlock`: text runs
    land in a single `Gtk.Label` each (carrying the Pango markup
    produced by `MarkdownMarkup._walk`), code blocks land in the
    dedicated `_make_code_block_widget` box so they can render with a
    full-width background + language pill.

    Existing children are removed first so callers can call this on
    every streamed chunk. Gtk handles the rebuild cost well within the
    sizes agent replies reach; a dedicated diffing pass would buy
    little at the cost of a noticeably more complex consumer."""
    while True:
        child = container.get_first_child()
        if child is None:
            break
        container.remove(child)

    for block in blocks:
        if isinstance(block, CodeBlock):
            container.append(_make_code_block_widget(block))
        else:
            container.append(
                _make_markdown_label(
                    block.markup,
                    css_classes=text_css_classes,
                    on_link=on_link,
                )
            )

class TurnCard:
    """One turn in the conversation. `role` is 'user' or 'assistant'.
    User cards get populated once via `set_text`; assistant cards stream
    chunk-by-chunk via `append`. Backed by `Gtk.Label` with Pango markup
    — labels measure synchronously so cards size correctly on the first
    layout pass (TextView doesn't, which used to leave user cards
    collapsed until the assistant reply forced a re-layout)."""

    THINKING_LABEL_STREAMING = "󰧮 thinking…"
    THINKING_LABEL_DONE = "󰧮 thinking"
    PLAN_LABEL_STREAMING = "󰃃 plan"
    PLAN_LABEL_DONE = "󰃃 plan · done"

    # Per-status glyph on each tool bubble. Intentionally text-only so
    # these render at the card font size without Pango fighting an
    # inline `<tt>` or PangoAttrList.
    _TOOL_STATUS_GLYPHS = {
        "pending": "⋯",
        "running": "⋯",
        "completed": "󰄬",
        "failed": "󰀦",
        "cancelled": "󰅖",
    }

    def __init__(
        self,
        role: str,
        title: str,
        on_link,
        tool_formatters: Optional[ToolFormatters] = None,
    ):
        self.role = role
        self.widget = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        self.widget.add_css_class("pilot-card")
        self.widget.add_css_class(f"pilot-card-{role}")

        self._role_label = Gtk.Label(label=title, xalign=0.0)
        self._role_label.add_css_class("pilot-card-role")
        self._role_label.add_css_class(f"pilot-card-role-{role}")
        self.widget.append(self._role_label)

        self._md = MarkdownMarkup()
        self._on_link = on_link
        # Per-adapter formatter instance. None → use a plain
        # `ToolFormatters()` with baseline defaults; consumers that
        # know which adapter produced the call pass
        # `adapter.tool_formatters` so opencode-specific tools
        # (codesearch, lsp, skill, question) render properly.
        self._tool_formatters: ToolFormatters = (
            tool_formatters if tool_formatters is not None else ToolFormatters()
        )
        self._text = ""
        self._thinking_text = ""
        # Lazily built when the first ThinkingChunk arrives — assistant
        # cards that never surface reasoning stay structurally
        # identical to user cards.
        self._thinking_expander: Optional[Gtk.Expander] = None
        self._thinking_label: Optional[Gtk.Label] = None
        self._thinking_collapsed = False
        # Plan state — lazily built on first `AgentPlanUpdate`. Agents
        # re-emit the full plan per-tick, so `_plan_items` holds the
        # most recent snapshot for the Ctrl+O "reopen last plan" hook
        # on the window.
        self._plan_expander: Optional[Gtk.Expander] = None
        self._plan_label: Optional[Gtk.Label] = None
        self._plan_items: list = []

        # Tool-bubble strip — lazily built when the first ToolCall
        # event arrives for this card. `_tool_bubbles_container` is
        # the outer vertical Box (bubbles row + stacked detail
        # panels); `_tool_bubbles_flow` is the FlowBox that actually
        # lays out the pill buttons. Bubbles are keyed by tool_id so
        # later status updates (pending → completed / cancelled)
        # find the right slot to rewrite.
        self._tool_bubbles_container: Optional[Gtk.Box] = None
        self._tool_bubbles_flow: Optional[Gtk.FlowBox] = None
        self._tool_details_box: Optional[Gtk.Box] = None
        self._tool_bubbles: dict[str, dict] = {}
        self._tool_bubbles_frozen = False

        # Replaces the single `Gtk.Label` that used to own the card
        # body. The body is now a vertical box rebuilt on every
        # `append` / `set_text` from `MarkdownMarkup.render_blocks`
        # output — each text run lands in its own Label (so Pango
        # layout stays cheap) and each fenced code block becomes a
        # `_make_code_block_widget` box with a full-width background
        # and a right-aligned language pill. `pilot-card-text` still
        # adds horizontal padding to the surrounding container so
        # prose text keeps the old inset look.
        self._body = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, hexpand=True, spacing=0
        )
        self._body.add_css_class("pilot-card-body")
        self.widget.append(self._body)
        # Link callback stashed so `_rebuild_markdown_body` can connect
        # each emitted text Label through it — user clicks on a
        # rendered `<a href="…">` should still fire the compositor-
        # specific opener rather than the default xdg-open path.
        self._on_link_cb = on_link

    def _render_body(self) -> None:
        """Re-walk `self._text` and rebuild the card body widgets in
        place. Cheap enough to run on every streamed chunk at the
        sizes agent responses reach; a diffing pass would buy little
        complexity savings given Gtk's measure-on-allocate path."""
        blocks = self._md.render_blocks(self._text)
        _rebuild_markdown_body(
            self._body,
            blocks,
            text_css_classes=("pilot-card-text",),
            on_link=self._on_link_cb,
        )

    def append(self, chunk: str) -> None:
        # First visible reply chunk — auto-collapse any thinking block
        # so the user's eye doesn't have to scroll past reasoning to
        # see the answer.
        if (
            not self._text
            and self._thinking_expander is not None
            and not self._thinking_collapsed
        ):
            self._thinking_expander.set_expanded(False)
            self._thinking_expander.set_label(self.THINKING_LABEL_DONE)
            self._thinking_collapsed = True
        self._text += chunk
        self._render_body()

    def set_text(self, text: str) -> None:
        self._text = text
        self._render_body()

    def append_thinking(self, chunk: str) -> None:
        """Append a reasoning chunk to the card's thinking section,
        creating the collapsible expander on first arrival. While
        streaming, the expander is open so the user sees reasoning
        land live; `append()` closes it once the real reply starts.

        If thinking arrives AFTER text has begun (mid-turn reasoning:
        some models emit thinking between tool calls or after a
        partial answer), we re-open the expander + flip its label
        back to STREAMING so the new reasoning is visible instead of
        hidden behind a `DONE` fold. The card's `_thinking_collapsed`
        flag is reset so the next text chunk will re-collapse cleanly
        once this reasoning batch ends."""
        if self._thinking_expander is None:
            self._thinking_label = Gtk.Label(
                xalign=0.0,
                yalign=0.0,
                hexpand=True,
                wrap=True,
                wrap_mode=Pango.WrapMode.WORD_CHAR,
                use_markup=True,
                selectable=True,
                natural_wrap_mode=Gtk.NaturalWrapMode.WORD,
            )
            self._thinking_label.add_css_class("pilot-card-text")
            self._thinking_label.add_css_class("pilot-thinking-text")
            self._thinking_expander = Gtk.Expander(
                label=self.THINKING_LABEL_STREAMING,
                expanded=True,
            )
            self._thinking_expander.add_css_class("pilot-thinking-expander")
            self._thinking_expander.add_css_class("expanded")  # starts open
            self._thinking_expander.connect(
                "notify::expanded", self._on_expander_toggle
            )
            self._thinking_expander.set_child(self._thinking_label)
            # Slot the expander between the role label and the reply
            # label so the visual order is: role → thinking → reply.
            self.widget.insert_child_after(self._thinking_expander, self._role_label)
        else:
            # Mid-turn reasoning: force the fold open + restore the
            # live label so new thinking doesn't disappear behind a
            # previously-auto-collapsed expander.
            self._thinking_expander.set_expanded(True)
            self._thinking_expander.set_label(self.THINKING_LABEL_STREAMING)
            self._thinking_collapsed = False
        self._thinking_text += chunk
        assert self._thinking_label is not None
        self._thinking_label.set_markup(self._md.render(self._thinking_text))

    def get_text(self) -> str:
        return self._text

    def set_plan(self, items) -> None:
        """Render / re-render the plan section from an `AgentPlanUpdate`.
        Lazy on first call — a card that never gets a plan stays
        structurally identical to a user card. Each re-render replaces
        the contents so the `in_progress` → `completed` transitions
        just look like the item's glyph changing.

        Kept expanded while items are still `in_progress`; snaps
        closed once the plan is fully completed so the card auto-
        compacts. Same `Gtk.Expander` pattern the thinking section
        uses."""
        if not items:
            return
        if self._plan_expander is None:
            self._plan_label = Gtk.Label(
                xalign=0.0,
                yalign=0.0,
                hexpand=True,
                wrap=True,
                wrap_mode=Pango.WrapMode.WORD_CHAR,
                use_markup=True,
                selectable=True,
                natural_wrap_mode=Gtk.NaturalWrapMode.WORD,
            )
            self._plan_label.add_css_class("pilot-card-text")
            self._plan_label.add_css_class("pilot-plan-text")
            self._plan_expander = Gtk.Expander(
                label=self.PLAN_LABEL_STREAMING,
                expanded=True,
            )
            self._plan_expander.add_css_class("pilot-plan-expander")
            self._plan_expander.add_css_class("expanded")  # starts open
            self._plan_expander.connect(
                "notify::expanded", self._on_expander_toggle
            )
            self._plan_expander.set_child(self._plan_label)
            # Plan slots in between the thinking expander (if any) and
            # the reply label. Insert after whichever of role/thinking
            # is currently the last-inserted "above body" widget.
            anchor = self._thinking_expander or self._role_label
            self.widget.insert_child_after(self._plan_expander, anchor)
        self._plan_items = list(items)
        assert self._plan_label is not None
        self._plan_label.set_markup(self._render_plan_markup(self._plan_items))
        # Auto-collapse once every item is done; otherwise force the
        # fold OPEN so new in-progress items never hide behind a
        # previously-auto-collapsed plan. Agents that emit plans in
        # waves (complete batch 1 → close, then add batch 2 items →
        # need the section visible again) benefit from the re-expand.
        all_done = all(
            getattr(it, "status", "") == "completed" for it in self._plan_items
        )
        if self._plan_expander is not None:
            if all_done:
                self._plan_expander.set_expanded(False)
                self._plan_expander.set_label(self.PLAN_LABEL_DONE)
            else:
                self._plan_expander.set_expanded(True)
                self._plan_expander.set_label(self.PLAN_LABEL_STREAMING)

    @staticmethod
    def _render_plan_markup(items) -> str:
        """Markdown-ish list with per-item status glyph and priority
        tint. Feeds straight into Pango markup — we don't need the
        full markdown pipeline for this."""
        glyph_for_status = {
            "completed": "󰄬",
            "in_progress": "◐",
            "pending": "○",
        }
        tint_for_priority = {
            "high": "#e06c75",
            "medium": "#d19a66",
            "low": "#5c6370",
        }
        out: list[str] = []
        for item in items:
            glyph = glyph_for_status.get(getattr(item, "status", ""), "•")
            colour = tint_for_priority.get(getattr(item, "priority", ""), "#abb2bf")
            body = GLib.markup_escape_text(getattr(item, "content", "") or "")
            out.append(
                f'<span foreground="{colour}" weight="bold">{glyph}</span> {body}'
            )
        return "\n".join(out)

    def toggle_plan(self) -> bool:
        """Flip the plan expander. Returns True if a plan section
        exists, False otherwise."""
        if self._plan_expander is None:
            return False
        self._plan_expander.set_expanded(not self._plan_expander.get_expanded())
        return True

    def has_plan(self) -> bool:
        return self._plan_expander is not None

    def toggle_thinking(self) -> bool:
        """Flip the thinking expander's open/closed state. Returns
        True if there was a thinking block to toggle, False otherwise
        — callers scanning for the latest thinking card use the
        return value to stop once they find one."""
        if self._thinking_expander is None:
            return False
        self._thinking_expander.set_expanded(not self._thinking_expander.get_expanded())

        return True

    @staticmethod
    def _on_expander_toggle(expander: "Gtk.Expander", _pspec) -> None:
        """Mirror an expander's `expanded` state onto an `.expanded`
        CSS class so theming can accent-highlight the pill/header
        while its body is revealed. Same hook used for the thinking
        and plan expanders — CSS specifies the tint per
        `.pilot-thinking-expander.expanded` / `.pilot-plan-expander.expanded`."""
        if expander.get_expanded():
            expander.add_css_class("expanded")
        else:
            expander.remove_css_class("expanded")

    # -- Tool bubbles --------------------------------------------------

    def _ensure_tool_bubbles_container(self) -> None:
        """Lazily build the bubble strip + detail-panel area. Inserted
        below the role label and above both the thinking expander (if
        already present) and the reply label, so the visual order is:
        role → tool bubbles → thinking → reply."""
        if self._tool_bubbles_container is not None:
            return
        container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        container.add_css_class("pilot-tool-bubbles")

        flow = Gtk.FlowBox()
        flow.set_selection_mode(Gtk.SelectionMode.NONE)
        flow.set_homogeneous(False)
        flow.set_column_spacing(4)
        flow.set_row_spacing(4)
        flow.set_max_children_per_line(20)
        flow.add_css_class("pilot-tool-bubbles-flow")
        container.append(flow)

        # Detail panels go in their own vertical box so expanding one
        # doesn't shift bubble positions. Each panel is a Gtk.Revealer
        # wrapping a details label; the bubble button toggles the
        # revealer's `reveal-child` property.
        details_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        details_box.add_css_class("pilot-tool-bubble-details-box")
        container.append(details_box)

        # Slot the strip immediately after the role label.
        self.widget.insert_child_after(container, self._role_label)
        self._tool_bubbles_container = container
        self._tool_bubbles_flow = flow
        self._tool_details_box = details_box

    # Maximum characters shown on a tool-bubble pill. MCP tool names
    # like `mcp__mcphub__linear_kilic-dev__save_issue` can be ~50
    # chars — we still want the leaf (`save_issue`) on screen when the
    # bubble gets truncated, so 120 leaves comfortable headroom for
    # server+tool even on the longest MCP registry entries while
    # capping runaway names (some Playwright / grafana tools nest
    # another prefix and push past 80 chars). Truncation happens from
    # the LEFT so the tool leaf stays visible.
    _BUBBLE_LABEL_MAX_CHARS = 120

    def _format_bubble_label(self, name: str, status: str) -> str:
        glyph = self._TOOL_STATUS_GLYPHS.get(status, "⋯")
        label = name or "tool"
        # Left-truncate so the leaf tool name (the actionable bit) is
        # always on screen; the full identifier still lives in the
        # button's tooltip + the expanded detail panel.
        if len(label) > self._BUBBLE_LABEL_MAX_CHARS:
            label = "…" + label[-(self._BUBBLE_LABEL_MAX_CHARS - 1) :]
        return f"{label} {glyph}"

    def _update_bubble_widget(self, slot: dict) -> None:
        """Rewrite the bubble button label + status CSS class from
        the slot's current name/status. Called by both append and
        update to keep one rendering path."""
        button: Gtk.Button = slot["button"]
        button.set_label(self._format_bubble_label(slot["name"], slot["status"]))
        for cls in ("pending", "running", "completed", "failed", "cancelled"):
            if cls == slot["status"]:
                button.add_css_class(cls)
            else:
                button.remove_css_class(cls)

    def _render_bubble_details(self, slot: dict) -> None:
        """Write the args preview + result text into the slot's
        detail panel. Called lazily when the revealer opens and on
        every subsequent status/result update so the panel stays in
        sync.

        Args + result run through `ToolFormatters.format` +
        `MarkdownMarkup.render_blocks` so code blocks emerge as
        stand-alone widgets with a full-width background + language
        pill instead of an inline Pango span that only shades the
        glyph bounds."""
        header_label: Gtk.Label = slot["details_header"]
        body_box: Gtk.Box = slot["details_body"]
        name = slot.get("name") or ""
        # Header shows the short verb from `ToolFormatters.short_header`
        # (`Execute`, `Read`, `Edit`, …) rather than the verbose agent
        # title — the title content shows up in the body verbatim, so
        # the short header keeps the bubble panel readable instead of
        # wrapping a long command twice. The programmatic tool name
        # rides in a monospace suffix when it differs from the header.
        display = self._tool_formatters.short_header(name) if name else "tool"
        if name and name.lower() != display.lower():
            header_label.set_markup(
                f"<b>{GLib.markup_escape_text(display)}</b> "
                f"<tt>({GLib.markup_escape_text(name)})</tt>"
            )
        else:
            header_label.set_markup(f"<b>{GLib.markup_escape_text(display)}</b>")

        args = slot.get("arguments") or ""
        md_body = self._tool_formatters.format(name, args)
        result = slot.get("result")
        if result:
            # Separate the args block from the result with a small
            # `_result:_` marker so the two run visibly distinct in the
            # revealer. `result` renders through the same pipeline so a
            # tool that emits fenced output also gets a proper code
            # block with a language pill.
            md_body = f"{md_body}\n\n*result:*\n\n{result}"
        try:
            blocks = self._md.render_blocks(md_body) if md_body else []
        except Exception as e:
            log.warning("bubble markdown render failed: %s", e)
            blocks = []
        _rebuild_markdown_body(
            body_box,
            blocks,
            text_css_classes=("pilot-tool-bubble-details",),
        )

    def _on_bubble_clicked(self, _button, tool_id: str) -> None:
        slot = self._tool_bubbles.get(tool_id)
        if slot is None:
            return
        revealer: Gtk.Revealer = slot["revealer"]
        expanded = not revealer.get_reveal_child()
        # Refresh before revealing so the label shows the latest
        # args/result instead of whatever was written at first toggle.
        self._render_bubble_details(slot)
        revealer.set_reveal_child(expanded)
        # Accent-highlight the pill itself while its detail panel is
        # open. Same pattern as a focused / pressed control: the pill
        # reads as "this is the one you're looking at". CSS
        # `.pilot-tool-bubble.expanded` handles the tint (inherits
        # the status-based fg colour, swaps the bg to accent).
        button: Gtk.Button = slot["button"]
        if expanded:
            button.add_css_class("expanded")
        else:
            button.remove_css_class("expanded")

    def append_tool_bubble(self, call: ToolCall) -> None:
        """Add a bubble for a freshly-seen ToolCall, or merge a
        follow-up event into an existing bubble. `call.tool_id` is
        the merge key. Calling with the same id a second time
        updates the existing slot in-place (delegates to
        `update_tool_bubble`)."""
        if self._tool_bubbles_frozen:
            # Post-finalisation updates: still merge so late
            # completions from a cancelled turn reflect accurately.
            pass
        self._ensure_tool_bubbles_container()
        tool_id = call.tool_id or f"bubble-{len(self._tool_bubbles)}"
        existing = self._tool_bubbles.get(tool_id)
        if existing is not None:
            self.update_tool_bubble(
                tool_id,
                status=call.status,
                arguments=call.arguments,
            )
            return

        button = Gtk.Button()
        button.add_css_class("pilot-tool-bubble")
        # Tooltip prefers the human-readable title (Claude's
        # "Read README.md", opencode's "edit"); falls back to the
        # canonical name when the agent didn't supply one. Helps users
        # distinguish several bubbles for the same tool at a glance.
        button.set_tooltip_text(call.title or call.name or "")
        button.set_can_focus(True)

        # Detail panel lives in the details_box (below the flow),
        # wrapped in a Revealer so toggling doesn't reshuffle layout.
        # The Revealer now wraps a vertical `Gtk.Box` (header label +
        # body widgets) instead of a single Label, so code blocks can
        # render as their own full-width boxes with a language pill
        # — a single Pango span only shades the glyph bounds.
        details_header = Gtk.Label(
            xalign=0.0,
            yalign=0.0,
            hexpand=True,
            wrap=True,
            wrap_mode=Pango.WrapMode.WORD_CHAR,
            use_markup=True,
            selectable=True,
            natural_wrap_mode=Gtk.NaturalWrapMode.WORD,
        )
        details_header.add_css_class("pilot-tool-bubble-details")
        details_body = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, hexpand=True, spacing=0
        )
        details_body.add_css_class("pilot-tool-bubble-details-body")
        details_container = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, hexpand=True, spacing=0
        )
        details_container.append(details_header)
        details_container.append(details_body)
        revealer = Gtk.Revealer()
        revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        revealer.set_reveal_child(False)
        revealer.set_child(details_container)
        assert self._tool_details_box is not None
        self._tool_details_box.append(revealer)

        slot = {
            "tool_id": tool_id,
            "name": call.name or "",
            "title": call.title or "",
            "kind": call.kind or "",
            "arguments": call.arguments or "",
            "status": call.status or "pending",
            "result": None,
            "button": button,
            "revealer": revealer,
            "details_header": details_header,
            "details_body": details_body,
        }
        self._tool_bubbles[tool_id] = slot
        button.connect("clicked", self._on_bubble_clicked, tool_id)
        self._update_bubble_widget(slot)
        assert self._tool_bubbles_flow is not None
        self._tool_bubbles_flow.append(button)

    def update_tool_bubble(
        self,
        tool_id: str,
        status: Optional[str] = None,
        arguments: Optional[str] = None,
        result: Optional[str] = None,
    ) -> None:
        """Mutate an existing bubble by tool_id. Any of status/args/
        result may be None to leave that field unchanged. No-op if
        the id isn't known (a completion without a matching open
        event — shouldn't happen but we stay defensive)."""
        slot = self._tool_bubbles.get(tool_id)
        if slot is None:
            return
        if status:
            slot["status"] = status
        if arguments:
            slot["arguments"] = arguments
        if result is not None:
            slot["result"] = result
        self._update_bubble_widget(slot)
        revealer: Gtk.Revealer = slot["revealer"]
        if revealer.get_reveal_child():
            # Keep an open detail panel in sync with the latest
            # args / result as they arrive.
            self._render_bubble_details(slot)

    def freeze_tool_bubbles(self, *, cancelled: bool = False) -> None:
        """Called by the window when the turn finalises. Flips any
        still-pending/running bubble to `completed` (clean end) or
        `cancelled` (user/adapter cancelled the turn), and stamps
        the `.frozen` CSS class on the strip so it can render as
        static rather than live."""
        self._tool_bubbles_frozen = True
        terminal_state = "cancelled" if cancelled else "completed"
        for slot in self._tool_bubbles.values():
            if slot["status"] in ("pending", "running"):
                slot["status"] = terminal_state
                self._update_bubble_widget(slot)
        if self._tool_bubbles_container is not None:
            self._tool_bubbles_container.add_css_class("frozen")

    def collapse_reasoning_sections(self) -> None:
        """Fold the thinking + plan expanders closed when the turn
        finalises — the user wants to see the ANSWER by default, not
        the reasoning scaffolding. Idempotent: collapsing an already-
        collapsed expander is a no-op. Users can re-open either via
        Ctrl+T / Ctrl+O or by clicking the expander header."""
        if self._thinking_expander is not None and not self._thinking_collapsed:
            self._thinking_expander.set_expanded(False)
            self._thinking_expander.set_label(self.THINKING_LABEL_DONE)
            self._thinking_collapsed = True
        if self._plan_expander is not None:
            self._plan_expander.set_expanded(False)
            self._plan_expander.set_label(self.PLAN_LABEL_DONE)

_PERMISSION_MD = MarkdownMarkup()

class PermissionRow(Gtk.ListBoxRow):
    """A pending tool-use event rendered as a full-width card above the
    queue. Mirrors `QueueRow`'s structure: a wrapping body + an action
    strip with three buttons. Buttons are:

    * `󰄬 allow`  — dismiss the row; the call already happened server-
                   side / in-CLI, this just acknowledges it.
    * ` trust`  — add this tool name to the session allowlist so
                   future invocations skip the row entirely.
    * `󰅖 deny`   — cancel the in-flight turn (same path as Ctrl+D) and
                   stamp the assistant card with a cancelled marker.

    The UI is visibility-only: we don't have a protocol for gating the
    actual tool execution in any of our backends. Documented in the
    plan; user-facing copy keeps the verbs honest."""

    # Fraction of the overlay window height the per-row body scroller
    # is allowed to take before scroll engages. 0.5 leaves the queue /
    # compose strip visible even when a single row wants to show a
    # massive pasted Bash command. Mirrors the `width_fraction=0.4`
    # pattern `LayerOverlayWindow` uses for overlay width; the host
    # calls `apply_height_fraction` with `get_allocated_height()` once
    # the overlay has laid out.
    BODY_HEIGHT_FRACTION = 0.5

    def __init__(
        self,
        call: ToolCall,
        on_allow,
        on_trust,
        on_deny,
        on_auto_reject=None,
        tool_formatters: Optional[ToolFormatters] = None,
    ):
        super().__init__()
        self._call = call
        self._on_allow = on_allow
        self._on_trust = on_trust
        self._on_deny = on_deny
        self._on_auto_reject = on_auto_reject
        self.add_css_class("pilot-permission-row")

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        card.add_css_class("pilot-permission-card")

        # Tool name — accent-coloured header. Shows the short verb-
        # style header from `ToolFormatters.short_header` (`Execute`,
        # `Read`, `Edit`, …) instead of the verbose `call.title` Claude
        # ships — the title content re-appears inside the body below
        # verbatim, so duplicating it in the header wastes real estate
        # on wrapping a long command. The tooltip carries the canonical
        # programmatic `call.name` so trust decisions stay traceable.
        formatters = (
            tool_formatters if tool_formatters is not None else ToolFormatters()
        )
        full_name = call.name or "(unnamed tool)"
        header = formatters.short_header(full_name)
        tooltip = full_name if full_name == header else f"{header}\n({full_name})"
        name_label = Gtk.Label(
            label=header,
            xalign=0.0,
            hexpand=True,
            wrap=True,
            wrap_mode=Pango.WrapMode.WORD_CHAR,
            natural_wrap_mode=Gtk.NaturalWrapMode.WORD,
            selectable=True,
        )
        name_label.add_css_class("pilot-permission-tool-name")
        name_label.set_tooltip_text(tooltip)
        card.append(name_label)

        # Argument preview. Routes through the adapter's
        # `ToolFormatters.format` so common tools (Bash / Read /
        # Edit / …) land in fenced code blocks with appropriate
        # language tags — the full command or diff is visible,
        # word-wrapped, never truncated. Rendering goes through
        # `_rebuild_markdown_body` so code blocks become dedicated
        # widgets with a full-width background + language pill;
        # pango-markup text runs land in their own labels whose wrap
        # is bounded only by the sidebar width (no line cap). Wrapped
        # in a ScrolledWindow capped at `BODY_HEIGHT_FRACTION` of the
        # overlay window height so a long pasted Bash command can't
        # push the row off-screen.
        md_body = formatters.format(call.name or "", call.arguments or "")
        args_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, hexpand=True, spacing=0
        )
        args_box.add_css_class("pilot-permission-args")
        # Tooltip keeps the raw (non-rendered) markdown — handy for
        # copy / paste when the user wants to round-trip the exact
        # argument payload elsewhere.
        args_box.set_tooltip_text(md_body)
        try:
            blocks = _PERMISSION_MD.render_blocks(md_body) if md_body else []
        except Exception as e:
            log.warning("permission markdown render failed: %s", e)
            blocks = []
        _rebuild_markdown_body(
            args_box,
            blocks,
            text_css_classes=("pilot-permission-args-text",),
        )
        self._args_scroller = Gtk.ScrolledWindow(hexpand=True)
        self._args_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._args_scroller.add_css_class("pilot-permission-args-scroller")
        self._args_scroller.set_child(args_box)
        # Until the host calls `apply_height_fraction` with the
        # overlay's real height, shrink to natural content — better
        # than guessing a fixed pixel cap that breaks on tall
        # monitors.
        self._args_scroller.set_propagate_natural_height(True)
        card.append(self._args_scroller)

        actions = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=4,
            halign=Gtk.Align.END,
        )
        actions.add_css_class("pilot-permission-actions")

        self._allow_btn = Gtk.Button(label="󰄬 allow")
        self._allow_btn.add_css_class("pilot-permission-allow")
        self._allow_btn.set_tooltip_text("Dismiss this tool-use notification")
        self._allow_btn.connect("clicked", lambda _b: self._on_allow(self))
        actions.append(self._allow_btn)

        trust_btn = Gtk.Button(label=" trust")
        trust_btn.add_css_class("pilot-permission-trust")
        trust_btn.set_tooltip_text(
            "Trust this tool for the rest of the session — future calls "
            "will be auto-dismissed without a prompt"
        )
        trust_btn.connect("clicked", lambda _b: self._on_trust(self))
        actions.append(trust_btn)

        self._deny_btn = Gtk.Button(label="󰅖 deny")
        self._deny_btn.add_css_class("pilot-permission-deny")
        self._deny_btn.set_tooltip_text("Cancel the current turn")
        self._deny_btn.connect("clicked", lambda _b: self._on_deny(self))
        actions.append(self._deny_btn)

        # 󰂭 auto-reject — symmetric to trust but for the auto-reject
        # list: future calls for this tool short-circuit to `deny`
        # without surfacing a row, AND the current turn is cancelled
        # so the model stops mid-sentence. Red-tinted like deny to
        # signal "this is destructive in both directions".
        self._auto_reject_btn: Optional[Gtk.Button] = None
        if on_auto_reject is not None:
            self._auto_reject_btn = Gtk.Button(label="󰂭 auto-reject")
            self._auto_reject_btn.add_css_class("pilot-permission-autoreject")
            self._auto_reject_btn.set_tooltip_text(
                "Cancel this turn AND auto-reject every future call to "
                "this tool for the rest of the session"
            )
            self._auto_reject_btn.connect(
                "clicked", lambda _b: self._on_auto_reject(self)
            )
            actions.append(self._auto_reject_btn)

        card.append(actions)
        self.set_child(card)

    def focus_allow(self) -> None:
        """Grab focus on the `󰄬 allow` button so keyboard users can
        accept without mousing. Tab cycles to trust / deny peers via
        GTK's default focus chain (all three buttons are focusable)."""
        self._allow_btn.grab_focus()

    def apply_height_fraction(self, window_height_px: int) -> None:
        """Cap the args-body scroller at `BODY_HEIGHT_FRACTION` of the
        given window height. Host calls this after the overlay has
        allocated so we have a real number to fraction against.
        Idempotent — safe to call on every resize."""
        if window_height_px <= 0:
            return
        cap = int(window_height_px * self.BODY_HEIGHT_FRACTION)
        if cap <= 0:
            return
        self._args_scroller.set_max_content_height(cap)
        self._args_scroller.set_propagate_natural_height(True)

    @property
    def tool_name(self) -> str:
        """Canonical tool identifier used for trust / auto-reject set
        membership. Prefers `call.name` when it looks programmatic (a
        single token, or the `mcp__server__tool` wire shape), otherwise
        falls back to the ACP `kind` so clicking 󰄬 trust on Claude's
        `"Read README.md"` row trusts ALL future Read calls via the
        `read` kind — not just the specific README invocation."""
        name = (self._call.name or "").strip()
        kind = (self._call.kind or "").strip()
        if name.startswith("mcp__"):
            return name
        # Programmatic identifier heuristic: no whitespace / path
        # separators / dot-qualified paths. Claude's built-in tool
        # titles ("Read foo.py", "git status", "$ ls -la") always
        # contain one of these characters, so they drop through to
        # the kind fallback.
        if name and not any(c in name for c in " \t\n\r/."):
            return name
        return kind or name

    @property
    def call(self) -> ToolCall:
        return self._call

# Tokens we insert into the compose via the resource palette. The parser
# in `PilotWindow.dispatch_turn` and the pre-check logic in
# `_preseed_resource_active_from_compose` both use this regex — keep the
# character class permissive enough for nested paths
# (`file/sub/dir/foo.py`) but tight enough that unrelated `#{…}` text in
# the user's message doesn't get swept up. The kind token restricts to
# `skill` / `file` / `mcp:*` etc.
_RESOURCE_TOKEN_RE = re.compile(r"#\{(?P<kind>[A-Za-z0-9_.:-]+)/(?P<name>[^}]+)\}")

def _format_resource_token(kind: str, name: str) -> str:
    """Canonical `#{<kind>/<name>}` wire form for the compose box. Kept
    in one place so the palette inserts the same shape the dispatch
    pre-filter looks for."""
    return "#{" + kind + "/" + name + "}"

def _preseed_resource_active_from_compose(
    compose: "ComposeView",
    resources: list[tuple[str, str, str, str]],
) -> set[tuple[str, str]]:
    """Scan the compose text for any `#{kind/name}` tokens that match a
    known resource and return the matched (kind, name) pairs. Unknown
    tokens are ignored — the dispatch pre-filter logs + strips them
    anyway. Pilot-specific: the `#{…}` wire shape is our token format,
    not the generic palette's concern."""
    text = compose.get_text()
    known = {(k, n) for k, n, _d, _p in resources}
    active: set[tuple[str, str]] = set()
    for match in _RESOURCE_TOKEN_RE.finditer(text):
        pair = (match.group("kind"), match.group("name"))
        if pair in known:
            active.add(pair)

    return active

def _commit_resources_to_compose(
    compose: "ComposeView",
    active_entries: list[tuple[str, str, str, str]],
) -> None:
    """Palette commit callback. Strip any pre-existing `#{kind/name}`
    tokens from the compose buffer (so toggling a resource off is a
    real remove, not an additive no-op) then insert the fresh set at
    the current cursor position. Pilot-specific: the token format and
    the spacing policy (prefix a space when the cursor sits on a
    non-whitespace character) are both our concern, not the generic
    palette's."""
    buf = compose._textview.get_buffer()
    existing = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)
    stripped = _RESOURCE_TOKEN_RE.sub("", existing)
    # Collapse double-spaces left behind by the strip — keeps the
    # textual flow clean when a user toggles off every resource.
    stripped = re.sub(r"[ \t]{2,}", " ", stripped)
    buf.set_text(stripped)

    tokens = [_format_resource_token(k, n) for (k, n, _d, _p) in active_entries]
    if tokens:
        joined = " ".join(tokens)
        cursor = buf.get_iter_at_mark(buf.get_insert())
        # Prefix a space if the existing text doesn't already end on
        # whitespace so tokens don't fuse with the previous word.
        before_iter = cursor.copy()
        prefix = ""
        if before_iter.backward_char():
            ch = buf.get_text(before_iter, cursor, True)
            if ch and ch not in (" ", "\n", "\t"):
                prefix = " "
        buf.insert(cursor, prefix + joined + " ")

    # Hand focus back to the compose textview so typing resumes
    # where the user left off.
    compose.focus()

class PilotWindow(LayerOverlayWindow):
    """Layer-shell sidebar. Conversation is a vertical stack of TurnCard
    widgets (one per user/assistant turn), queued turns are cards of
    their own above the compose, and compose is a multi-line TextView
    with a visible SEND button. Phases (idle/pending/streaming) surface
    in both the header pill and the waybar module."""

    USER_TITLE = "Retarded Peasant"
    ASSISTANT_TITLE_FMT = "AI Overlord - {provider}"
    ASSISTANT_TITLE_WITH_MODEL_FMT = "AI Overlord - {provider} ({model})"
    HEADER_FMT = "Pilot - {provider}"
    HEADER_WITH_MODEL_FMT = "Pilot - {provider} ({model})"

    def __init__(
        self,
        app: Gtk.Application,
        adapter: ConversationAdapter,
        auto_approve: Optional[list[str]] = None,
        auto_reject: Optional[list[str]] = None,
        cwd: Optional[str] = None,
        skills_dir: Optional[str] = None,
        mcp_server_names: Optional[list[str]] = None,
        session_suffix: str = "",
    ):
        # LayerOverlayWindow handles Gtk4LayerShell init, anchors,
        # keyboard mode, and the initial width sizing. Pilot's shape
        # (top+bottom+right → full-height sidebar, 40% width) matches
        # the scaffold's defaults, but we pass them explicitly so
        # future scripts reading this call-site can see exactly what
        # a pilot-style overlay needs.
        super().__init__(
            application=app,
            title="Pilot",
            namespace="pilot",
            anchors=("top", "bottom", "right"),
            width_fraction=0.4,
            fallback_width=520,
        )
        self._app = app
        self._adapter = adapter
        # Session handle wired later via `attach_session` — the overlay
        # uses it to push auto-list mutations into the authoritative
        # Session state (which the MCP subprocess polls over the bridge
        # socket). None-safe so tests can construct windows without a
        # live socket.
        self._session: Optional["Session"] = None
        # Adapter may expose `model` (ACP adapters always do); empty /
        # missing → treat as absent.
        raw_model = getattr(adapter, "model", "") or ""
        self._model: Optional[str] = raw_model.strip() or None
        self._provider_name = adapter.provider.value
        self._streaming = False
        self._alive = True
        self._queue: list[QueueRow] = []
        self._phase: str = "idle"
        # `_stream_started` flips true on the first text chunk of a
        # turn; combined with `_streaming` and the permissions /
        # question-banner state it fully determines the effective
        # phase via `_update_phase()` — no caller needs to pick a
        # phase string directly.
        self._stream_started = False
        # Sticky per-turn flag. Set by the cancel paths (Ctrl+D,
        # deny, auto-reject) so `_mark_idle` knows to freeze the
        # assistant card's tool-bubble strip with the `cancelled`
        # terminal state rather than `completed`. Reset at the top
        # of every new turn.
        self._turn_cancelled = False
        self._cards: list[TurnCard] = []
        self._active_assistant: Optional[TurnCard] = None
        # Permission rows stack above the queue. `PermissionState`
        # tracks the three disjoint sets (trust / auto-approve /
        # auto-reject); `show_permission_for_acp` consults
        # `decide(name)` first and short-circuits without surfacing a
        # row when a tool is already in one of them.
        self._permissions: list[PermissionRow] = []
        self._permission = PermissionState.from_seeds(
            auto_approve=auto_approve or (),
            auto_reject=auto_reject or (),
        )
        # Palette seed data: `--cwd` / `--skills-dir` / the resolved
        # list of MCP server names. All three
        # can be None/empty — the palette's `_collect_resources` drops
        # empty sections gracefully.
        self._cwd: Optional[str] = cwd
        self._skills_dir: Optional[str] = skills_dir
        self._mcp_server_names: list[str] = list(mcp_server_names or [])
        self._session_suffix: str = session_suffix or ""
        # Agent-supplied session title (from ACP `session_info_update`
        # notifications). Populated by `_apply_session_info` and
        # appended to the header format string so users see the
        # real conversation summary alongside the provider/model.
        self._session_title: str = ""
        # Palette widget is built on demand (first Ctrl+Space) and then
        # cached — we reset state (search box, active set) on every
        # open so stale selections don't leak across sessions.
        self._palette: Optional[CommandPalette] = None
        self._references_palette: Optional[CommandPalette] = None
        self._permissions_palette: Optional[CommandPalette] = None
        self._mcp_palette: Optional[CommandPalette] = None
        self._models_palette: Optional[CommandPalette] = None
        self._modes_palette: Optional[CommandPalette] = None
        self._commands_palette: Optional[CommandPalette] = None
        self._sessions_palette: Optional[CommandPalette] = None
        self._cwd_palette: Optional[CommandPalette] = None
        self._keybindings_palette: Optional[CommandPalette] = None
        # Root palette (Ctrl+Space) — single-select index that opens
        # one of the leaf palettes (skills / MCPs / sessions) on
        # commit. Leaf palettes keep their own widgets because each
        # has bespoke commit / delete wiring; the root is just a
        # dispatcher that picks which leaf to raise next.
        self._root_palette: Optional[CommandPalette] = None
        # Resources the user has attached via the palette but hasn't
        # submitted yet. Each `(kind, name, description)` renders as a
        # pill above the compose hint; `dispatch_turn` inlines them at
        # submit time.
        self._pending_resources: list[tuple[str, str, str]] = []
        # Binary / image payloads the user has pasted (Ctrl+P) but
        # hasn't submitted yet. These ride on the next turn as ACP
        # content blocks prefixed to the text prompt — never inlined
        # into the display text, since they can't be flattened to a
        # readable string.
        self._pending_attachments: list[PromptAttachment] = []
        # Most-recent plan snapshot so Ctrl+O can reopen / scroll to
        # the latest `AgentPlanUpdate`. `_last_plan_card` is the
        # TurnCard that rendered it; `_last_plan_items` is the raw
        # PlanItem list for re-display after turn finalisation.
        self._last_plan_card: Optional["TurnCard"] = None
        self._last_plan_items: list = []
        self._install_css()

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.add_css_class("pilot-root")

        # Header --------------------------------------------------------
        # Two-row layout: provider/phase pill + close on the top row,
        # breadcrumb pills (cwd / mode / mcps / skills / restored) on
        # the bottom row. Separating the breadcrumb from the top row
        # gives the cwd pill the full header width to work with —
        # previously the cwd, mcps count, and restored tag competed
        # with the provider pill + close button for horizontal space
        # on the 400-ish-px sidebar and got clipped silently. The cwd
        # pill hexpands + middle-ellipsizes so it shrinks first when
        # the bottom row still overflows under aggressive width caps.
        header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        header.add_css_class("pilot-header")
        self._header = header

        # hexpand=True on top_row is LOAD-BEARING for the title-pill
        # truncation: without it, the Box sizes to the sum of its
        # children's natural widths and can push beyond the layer-shell
        # window bounds (then Pango has no bounded allocation to
        # ellipsize against, so the pill grows with the title text).
        # With hexpand=True the Box inherits the window's width and
        # the title pill's hexpand can actually flex within that.
        top_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
            hexpand=True,
        )
        top_row.add_css_class("pilot-header-top")
        # Provider pill stays at its natural width — "[suffix] Pilot -
        # provider (model)" is a known, bounded label so it doesn't
        # need to flex. The session-title label next to it is the flex
        # element that absorbs the remaining width.
        self._provider_label = Gtk.Label(
            label=self._header_title(),
            xalign=0.0,
        )
        self._provider_label.add_css_class("pilot-provider")
        self._provider_label.add_css_class("idle")
        top_row.append(self._provider_label)

        # Agent-supplied session title (from ACP `session_info_update`)
        # lives as its own pill on the top row. Always visible +
        # hexpand=True so the pill reserves the flex space between
        # the provider pill and the close button regardless of
        # whether the title is set — keeps the close button pinned
        # to the trailing edge even on session start / after
        # `start_fresh_session` before a title lands. The inner
        # label middle-ellipsizes at the tail when the title would
        # push the close button off-screen.
        # Plain Gtk.Label (NOT a pill button) so we fully own its
        # sizing. Wrapping in Gtk.Button caused the button to replay
        # its own natural-width calculation based on the auto-created
        # child label's text — which meant every `set_label()` call
        # in `_refresh_session_title_label` reset the max_width_chars
        # we'd carefully configured at init, letting the natural
        # request balloon with the title and pushing past the
        # layer-shell width bound. The label looks pill-shaped via
        # CSS (`.pilot-session-title` + `.empty`) — same visual
        # result without the Button container stealing control of
        # the size request.
        self._session_title_pill = Gtk.Label(
            label="",
            xalign=0.0,
            hexpand=True,
            halign=Gtk.Align.FILL,
        )
        self._session_title_pill.add_css_class("pilot-session-title")
        self._session_title_pill.add_css_class("empty")
        self._session_title_pill.set_ellipsize(Pango.EllipsizeMode.END)
        # GTK4 label sizing with ellipsize:
        #   - `width_chars`     → minimum allocation in chars
        #   - `max_width_chars` → NATURAL allocation in chars
        # Pin natural to 1 char so the label's contribution to the
        # top_row's natural width stays tiny — the row doesn't
        # balloon with the title length. `hexpand=True` + `halign=FILL`
        # let the label grow to whatever allocation is left after
        # provider + close pills, and Pango END-ellipsizes the tail
        # when even that allocation can't fit the full title.
        self._session_title_pill.set_width_chars(0)
        self._session_title_pill.set_max_width_chars(1)
        self._session_title_pill.set_size_request(1, -1)
        top_row.append(self._session_title_pill)

        close_btn = Gtk.Button(label="󰅖")
        close_btn.add_css_class("pilot-close")
        close_btn.connect("clicked", lambda _b: self.close())
        top_row.append(close_btn)
        header.append(top_row)

        # Breadcrumb — one row of pills (cwd / mode / mcps / skills /
        # restored). Kept as Gtk.Box not Label so each segment can get
        # its own CSS tint (`restored` glows yellow so it stands out).
        # Sits under the provider pill as the second header row.
        self._session_pills = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=4,
            hexpand=True,
        )
        self._session_pills.add_css_class("pilot-session")
        self._session_pills.set_tooltip_text(self._session_subtitle(verbose=True))
        header.append(self._session_pills)

        root.append(header)

        # Dismissable error toast — hidden until an ACP-side failure
        # fires `show_error`. Ctrl+E clears it. Sits right below the
        # header so errors never push the conversation scroller down.
        self._toast = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
            visible=False,
        )
        self._toast.add_css_class("pilot-toast")
        self._toast_label = Gtk.Label(xalign=0.0, hexpand=True)
        self._toast_label.set_wrap(True)
        self._toast_label.set_wrap_mode(2)  # WORD_CHAR
        self._toast_label.set_max_width_chars(1)
        self._toast_label.add_css_class("pilot-toast-text")
        self._toast.append(self._toast_label)
        toast_close = Gtk.Button(label="󰅖")
        toast_close.add_css_class("pilot-toast-close")
        toast_close.set_tooltip_text("Dismiss (Ctrl+E)")
        toast_close.connect("clicked", lambda _b: self.dismiss_error())
        self._toast.append(toast_close)
        root.append(self._toast)

        # Conversation: a vertical box of TurnCard widgets inside a
        # scroller. Each card is its own markdown-rendered surface.
        self._conv_scroller = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        # NEVER horizontal so the child Box fills the viewport width
        # instead of collapsing to its minimum natural width — that was
        # making cards render as narrow slivers (sometimes invisible
        # entirely when the short role label was the only measurable
        # natural-width content).
        self._conv_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._conv_scroller.add_css_class("pilot-conv-scroller")
        self._conv_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        self._conv_box.add_css_class("pilot-conv")
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
        # page-size shrinks whenever the compose grows (multi-line
        # submissions, permission row appears) — re-pin in that case
        # too, otherwise the viewport cuts off the bottom of the
        # newest card.
        vadj.connect("notify::page-size", self._on_vadj_upper_changed)
        # Frame-tick-driven autofollow. Content events (new chunk, new
        # tool bubble, plan update, thinking expand) call
        # `_arm_autofollow()`, which registers a GDK frame tick callback
        # that re-pins the scrollbar to the bottom on every vblank for
        # a short window. This beats the idle-add retry strategy we
        # had before, which kept losing races against multi-pass
        # markdown / Pango label re-measurement — the tick runs once
        # per frame REGARDLESS of how many more reflows GTK queues,
        # so a long reply + tool bubbles + thinking expander can't
        # out-pace us. Deadline is extended every time new content
        # arrives, so a continuous stream keeps the window alive as
        # long as data is flowing.
        self._autofollow_deadline: float = 0.0
        self._autofollow_tick_id: Optional[int] = None
        # Guards against unpinning when WE called `set_value(bottom)`.
        # Without this, programmatic scrolls race the reflow: we set
        # value = stale-bottom, upper grows immediately after, and the
        # value-changed handler sees "you're above bottom by more than
        # the threshold" and flips _pinned=False for the rest of the
        # turn.
        self._programmatic_scroll = False

        # Permissions ---------------------------------------------------
        # Sits above the queue so pending tool-use notifications take
        # priority visually — they're transient and often need a
        # response before the user cares about queued turns.
        self._permissions_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._permissions_box.add_css_class("pilot-permissions")
        permissions_header = Gtk.Label(label="TOOLS", xalign=0.0)
        permissions_header.add_css_class("pilot-permissions-header")
        self._permissions_box.append(permissions_header)
        self._permissions_listbox = Gtk.ListBox()
        self._permissions_listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self._permissions_box.append(self._permissions_listbox)
        self._permissions_box.set_visible(False)
        root.append(self._permissions_box)

        # Queue ---------------------------------------------------------
        self._queue_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._queue_box.add_css_class("pilot-queue")
        queue_header = Gtk.Label(label="QUEUED", xalign=0.0)
        queue_header.add_css_class("pilot-queue-header")
        self._queue_box.append(queue_header)
        self._queue_listbox = Gtk.ListBox()
        self._queue_listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self._queue_box.append(self._queue_listbox)
        self._queue_box.set_visible(False)
        root.append(self._queue_box)

        # Compose -------------------------------------------------------
        self._compose = ComposeView(on_submit=self.dispatch_turn)
        root.append(self._compose.widget)

        # Root-level overlay so the command palette can float across
        # the FULL window height (50% of monitor) instead of being
        # caged inside the ~25%-capped compose box. `_compose_overlay`
        # still exists for compose-local overlays (nothing uses it
        # today, but keeping the name stable lets the palette helpers
        # stay readable).
        self._compose_overlay = Gtk.Overlay()
        self._compose_overlay.set_child(root)
        self.set_child(self._compose_overlay)

        # Render pills for the seeded trust + auto-list state so the
        # user sees pre-authorised / auto-decided tools from the moment
        # the window opens. `_sync_permission_state` is the full tri-
        # list sync; the older `_sync_tool_gate` alias stays available
        # for back-compat.
        self._sync_permission_state()

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

    def _expand_resource_tokens(self, text: str) -> str:
        """Replace every `#{kind/name}` in `text` with the referenced
        resource body (skill / reference), then strip leftover tokens
        for unknown kinds. Missing resources collapse to an inline
        `-- resource unavailable: … --` note rather than crashing the
        turn, so a stale palette pick still produces a valid prompt."""
        resolved: list[str] = []
        for match in _RESOURCE_TOKEN_RE.finditer(text):
            kind, name = match.group("kind"), match.group("name")
            body = self._resolve_resource(kind, name)
            if body is not None:
                resolved.append(f"### {kind}/{name}\n\n{body}")
        if not resolved:
            return _RESOURCE_TOKEN_RE.sub("", text).strip()
        user_tail = _RESOURCE_TOKEN_RE.sub("", text)
        user_tail = re.sub(r"[ \t]{2,}", " ", user_tail).strip()
        sections = "\n\n".join(resolved)
        if user_tail:
            return f"{sections}\n\n---\n\n{user_tail}"
        return sections

    def _resolve_resource(self, kind: str, name: str) -> Optional[str]:
        """Load the body of a palette-picked resource. Returns None when
        the kind is unknown or the file is unreadable — caller drops
        the token in that case.

        `name` may be either the programmatic slug (legacy `#{skill/
        <slug>}` compose tokens) or the display title (palette entries
        built from `list_skills_via_mcp` since we started surfacing
        `skill.title` at the top). Slug wins — cheap `os.path.isdir`
        check — with a title fallback that scans `load_skills` so
        rewriting the palette→pending flow to carry both isn't needed."""
        if not self._skills_dir:
            return None
        if kind == "skill":
            slug = self._resolve_skill_slug(name)
            if slug is None:
                return None
            skill_md = os.path.join(self._skills_dir, slug, "SKILL.md")
            skill = parse_skill(skill_md, fallback_name=slug)
            if skill is None:
                return None
            refs = load_skill_references(self._skills_dir, slug)
            if refs and refs.startswith("No references"):
                refs = None
            parts = [skill.body]
            if refs:
                parts.append("### References\n\n" + refs)
            return "\n\n".join(parts)
        if kind == "reference":
            return read_reference(self._skills_dir, name)
        return None

    def _resolve_skill_slug(self, name: str) -> Optional[str]:
        """Map a palette entry's `name` — which may be either the
        programmatic slug or the display title — back to the skill
        directory slug. Preserves the legacy slug path (one `isdir`
        call) and only walks the full skills list on title misses."""
        skills_dir = self._skills_dir
        if not skills_dir:
            return None
        direct = os.path.join(skills_dir, name, "SKILL.md")
        if os.path.isfile(direct):
            return name
        for skill in load_skills(skills_dir):
            if skill.title == name:
                return skill.name
        return None

    def stage_turn(self, user_message: str) -> bool:
        """Drop `user_message` into the compose TextView instead of
        dispatching it. Called by the socket handler when a new turn
        arrives while the overlay is already visible — the user reads
        the inserted text, edits if needed, then presses Enter to send.

        Existing compose content is preserved + the new text lands on
        a fresh line (appended paragraph-style); the cursor moves to
        the end so Enter submits everything.

        Also force-presents the overlay — useful when the overlay was
        visible on another workspace or was momentarily unfocused by
        another client at the moment the input arrived. Returns False
        so `GLib.idle_add` fires the scheduled call exactly once."""
        if not user_message:
            return False
        self._compose.stage_text(user_message)
        # Mirror `dispatch_turn`'s "ensure visible" block so stage works
        # even if the caller scheduled us during a visibility flicker.
        if not self.get_visible():
            self._bind_to_focused_monitor()
            self.set_visible(True)
        self.present()
        self._compose.focus()
        log.info(
            "stage_turn: chars=%d existing=%s",
            len(user_message),
            bool(self._compose.get_text()),
        )
        return False

    def dispatch_turn(self, user_message: str) -> None:
        # Two views of the turn:
        #   - `display`: the user's clean typed prose. Shown in the
        #     chat card.
        #   - `prompt`: display + every resource body (pill-attached or
        #     `#{kind/name}`-inlined) prepended as fenced sections.
        #     Handed to the adapter; the user never sees it.
        # Keeps the conversation readable — a skill attachment might
        # be 30KB of markdown, and dumping that into the card on every
        # submit made the chat unscrollable.
        display = user_message.strip()
        prompt = self._build_agent_prompt(user_message)
        attachments = list(self._pending_attachments)
        log.info(
            "dispatch_turn: display_len=%d prompt_len=%d resources=%d attachments=%d "
            "streaming=%s queue=%d",
            len(display),
            len(prompt),
            len(self._pending_resources),
            len(attachments),
            self._streaming,
            len(self._queue),
        )
        if self._pending_resources:
            self._pending_resources = []
            self._refresh_resource_pills()
        if self._pending_attachments:
            self._pending_attachments = []
            self._refresh_attachment_pills()
        # An attachment-only turn (pasted image, no prose) is still a
        # valid submission — the ACP prompt requires at least one
        # content block but the text block may be empty.
        if not prompt and not attachments:
            log.debug("dispatch_turn: nothing to send; dropping")
            return
        if not self.get_visible():
            self._bind_to_focused_monitor()
            self.set_visible(True)
            self.present()
        if self._streaming or self._queue:
            # Queue rows store the WIRE prompt (with resources) so the
            # drain later submits exactly what the user composed.
            # Display remains the clean prose.
            self._enqueue(prompt, display=display, attachments=attachments)
            return
        self._start_turn(prompt, display=display, attachments=attachments)

    def _build_agent_prompt(self, user_message: str) -> str:
        """Merge pill-attached resources + any inline `#{kind/name}`
        tokens with the user's typed text. Empty `user_message` plus
        no resources → empty string (caller drops the turn)."""
        body = self._expand_resource_tokens(user_message)
        sections: list[str] = []
        for kind, name, _desc in self._pending_resources:
            resolved = self._resolve_resource(kind, name)
            if resolved is not None:
                sections.append(f"### {kind}/{name}\n\n{resolved}")
        if not sections:
            return body.strip()
        tail = body.strip()
        joined = "\n\n".join(sections)
        return f"{joined}\n\n---\n\n{tail}" if tail else joined

    def phase(self) -> str:
        return self._phase

    def is_streaming(self) -> bool:
        return self._streaming

    def queue_size(self) -> int:
        return len(self._queue)

    def adapter(self) -> ConversationAdapter:
        """Read-only accessor for the wrapped ConversationAdapter. Used
        by the socket-status handler to surface the model name without
        reaching past the window boundary."""
        return self._adapter

    # -- Turn lifecycle -------------------------------------------------

    def _start_turn(
        self,
        message: str,
        *,
        display: Optional[str] = None,
        attachments: Optional[list[PromptAttachment]] = None,
    ) -> None:
        """`message` is the wire prompt handed to the adapter (may
        contain inlined resource bodies); `display` is the clean text
        rendered in the user card. `attachments` is an optional list
        of binary content blocks (pasted images today) that prefix
        the text block in the ACP prompt."""
        card_text = display if display is not None else message
        self._append_user_card(card_text)
        self._active_assistant = self._append_assistant_card()
        self._streaming = True
        self._stream_started = False
        self._turn_cancelled = False
        self._update_phase()
        threading.Thread(
            target=self._run_turn,
            args=(message,),
            kwargs={"attachments": list(attachments or [])},
            daemon=True,
        ).start()

    def _append_user_card(self, text: str) -> TurnCard:
        card = TurnCard(
            role="user",
            title=self.USER_TITLE,
            on_link=self._open_link,
            tool_formatters=self._tool_formatters(),
        )
        self._cards.append(card)
        # Explicit user action → always pin-to-bottom and scroll, even
        # if the user had scrolled up before clicking send.
        self._conv_box.append(card.widget)
        card.set_text(text)
        self._force_scroll_to_bottom()
        return card

    def _append_assistant_card(self) -> TurnCard:
        card = TurnCard(
            role="assistant",
            title=self._assistant_title(),
            on_link=self._open_link,
            tool_formatters=self._tool_formatters(),
        )
        self._cards.append(card)
        self._conv_box.append(card.widget)
        self._force_scroll_to_bottom()
        return card

    def _tool_formatters(self) -> Optional[ToolFormatters]:
        """Return the active adapter's `ToolFormatters` instance, or
        None when the adapter hasn't loaded one (callers fall back
        to a plain `ToolFormatters()` baseline). Factored out so
        `TurnCard` + `PermissionRow` share the same lookup rather
        than each digging into `self._adapter` on their own."""
        adapter = getattr(self, "_adapter", None)
        if adapter is None:
            return None
        return getattr(adapter, "tool_formatters", None)

    def _header_title(self) -> str:
        """Provider pill label — `[suffix] Pilot - provider (model)`.
        Kept short and predictable so it fits at natural width in the
        top row. The agent-supplied session title (populated by
        `_apply_session_info` from ACP `session_info_update`) rides on
        its own `_session_title_label` next to this pill, NOT inline,
        so it can flex to fill the remaining header width and only
        ellipsize when it genuinely overflows the window."""
        if self._model:
            base = self.HEADER_WITH_MODEL_FMT.format(
                provider=self._provider_name, model=self._model
            )
        else:
            base = self.HEADER_FMT.format(provider=self._provider_name)
        if self._session_suffix:
            base = f"[{self._session_suffix}] {base}"
        return base

    def _apply_session_info(self, title: str) -> bool:
        """Main-thread sink for `SessionInfoChunk` events. Stashes
        the agent-supplied title and repaints the dedicated title
        label next to the provider pill. Tolerates empty-string
        clears (ACP spec lets agents wipe the title by sending
        `null`) — the label hides itself when the title goes empty."""
        self._session_title = (title or "").strip()
        self._refresh_session_title_label()
        return False

    def _refresh_session_title_label(self) -> None:
        """Sync `self._session_title_pill` (a plain Gtk.Label styled
        like a pill via CSS) with `self._session_title`.

        The label stays visible + hexpanded at all times (including
        when no title is set) so the close button sits at the
        trailing edge regardless of whether an agent has pushed a
        session title yet. The `.empty` CSS class strips background +
        padding when the title is blank so it reads as a transparent
        spacer instead of a weird empty rounded rectangle.

        Tooltip carries the full string in case `ellipsize=END` is
        clipping the tail on a narrow sidebar."""
        if not hasattr(self, "_session_title_pill"):
            return
        label = self._session_title
        self._session_title_pill.set_text(label or "")
        self._session_title_pill.set_tooltip_text(label or "")
        if label:
            self._session_title_pill.remove_css_class("empty")
        else:
            self._session_title_pill.add_css_class("empty")

    def _effective_cwd(self) -> str:
        """Adapter cwd wins when set (it's the authoritative source
        after a palette-driven switch / session restore). Fall back
        to `self._cwd` (seeded from --cwd at spawn) and finally
        `os.getcwd()` so the breadcrumb never renders empty."""
        return getattr(self._adapter, "cwd", None) or self._cwd or os.getcwd()

    def _pretty_cwd(self) -> str:
        """Return a compact cwd label. Collapses `$HOME` to `~`, and
        when the full path is longer than 48 chars keeps only the last
        three segments (with a leading `…/`) so the breadcrumb stays
        one line on a ~400px-wide sidebar."""
        raw = self._effective_cwd()
        home = os.path.expanduser("~")
        if raw.startswith(home):
            raw = "~" + raw[len(home) :]
        if len(raw) <= 48:
            return raw
        parts = raw.split(os.sep)
        if len(parts) <= 3:
            return raw
        tail = os.sep.join(parts[-3:])
        return f"…/{tail}"

    def _session_subtitle(self, *, verbose: bool = False) -> str:
        """Single-line textual breadcrumb — used as the pill-row
        tooltip so hover shows the full path + segments at once.
        `verbose` swaps the truncated cwd for the untruncated one."""
        cwd = self._effective_cwd() if verbose else self._pretty_cwd()
        parts = [f"@ {cwd}"]
        mode = (getattr(self._adapter, "current_mode_id", None) or "").strip()
        if mode:
            parts.append(f"mode {mode}")
        if self._mcp_server_names:
            parts.append(f"+{len(self._mcp_server_names)} mcps")
        if self._skills_dir:
            parts.append("+skills")
        if getattr(self._adapter, "session_resumed", False):
            parts.append("󰑐 restored")
        return "  ".join(parts)

    def _refresh_session_label(self) -> None:
        """Rebuild the header breadcrumb as a pill row.

        Segments:
          - `@ <cwd>`                muted pill
          - `+N mcps` (if any)       muted pill
          - `+skills` (if any)       muted pill
          - `󰑐 restored` (if resumed) WARN pill — yellow so the user
                                      notices resumed state without
                                      reading the row character-by-
                                      character.

        Also pulls the adapter's current model onto `self._model` and
        repaints the header pill (`Pilot - <provider> (<model>)`) so
        `reconcile_model` after a turn, `set_model` from the Models
        palette, and session restores all land on the window title
        without a full respawn."""
        effective = (
            getattr(self._adapter, "current_model_id", None)
            or getattr(self._adapter, "model", None)
            or ""
        )
        self._model = effective.strip() or None
        # Repaint the provider pill — but only when it's not currently
        # showing the `WORKING` spinner, so a model switch fired from
        # the palette doesn't blow away the working-state label in
        # the same tick. `_clear_working` restores the title itself.
        if hasattr(self, "_provider_label") and not self._provider_label.has_css_class(
            "working"
        ):
            self._provider_label.set_label(self._header_title())

        if not hasattr(self, "_session_pills"):
            return
        # Wipe + rebuild; cheaper than diffing. The row has 2-4 pills.
        child = self._session_pills.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._session_pills.remove(child)
            child = nxt

        # cwd is the ONE pill whose label is user-sized — a long path
        # can easily exceed the sidebar width on its own even after
        # `_pretty_cwd`'s 48-char cap. Build it with ellipsize so the
        # Pango label shrinks from the middle when the total row
        # overflows; the other pills stay at natural width.
        cwd_pill = self._make_ellipsizing_cwd_pill(f"@ {self._pretty_cwd()}")
        self._session_pills.append(cwd_pill)

        tail: list[tuple[str, str]] = []
        mode = (getattr(self._adapter, "current_mode_id", None) or "").strip()
        if mode:
            # Mode drifts silently on some agents (claude flips out of
            # plan-mode on plan accept, opencode relabels on agent
            # switch) — surfacing it here means the post-turn reconcile
            # pulls the new label into the header without a restart.
            tail.append((f"󰢻 {mode}", PillVariant.MUTED))
        if self._mcp_server_names:
            tail.append((f"+{len(self._mcp_server_names)} mcps", PillVariant.MUTED))
        if self._skills_dir:
            tail.append(("+skills", PillVariant.MUTED))
        if getattr(self._adapter, "session_resumed", False):
            tail.append(("󰑐 restored", PillVariant.WARN))

        for label, variant in tail:
            pill = make_pill(label, variant)
            pill.set_sensitive(False)  # Breadcrumb is informational, not actionable.
            self._session_pills.append(pill)
        self._session_pills.set_tooltip_text(self._session_subtitle(verbose=True))

    def _make_ellipsizing_cwd_pill(self, label: str) -> Gtk.Button:
        """Build the cwd breadcrumb pill as the row's flex element:
        hexpands to grab all remaining width, middle-ellipsizes its
        inner Label so the most meaningful tail (project / file name)
        stays visible when the header row can't fit everything. Other
        breadcrumb pills (mcps / skills / restored) stay at natural
        width, so this is the one that absorbs the squeeze.

        `make_pill` returns a `Gtk.Button` whose first child is the
        caption `Gtk.Label`; we reach in directly because the factory
        doesn't expose ellipsize configuration — adding a kwarg there
        would affect every other pill call site in the overlay."""
        pill = make_pill(label, PillVariant.MUTED)
        pill.set_hexpand(True)
        pill.set_halign(Gtk.Align.FILL)
        pill.set_sensitive(False)  # Informational only.
        inner = pill.get_first_child()
        if isinstance(inner, Gtk.Label):
            inner.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            # max_width_chars=0 lets the label request as much width
            # as it naturally wants; ellipsize kicks in only when the
            # allocated width falls short of that request.
            inner.set_max_width_chars(0)
            inner.set_xalign(0.0)
            inner.set_hexpand(True)
        return pill

    # -- Working indicator -------------------------------------------------
    #
    # Some ACP RPCs (set_session_model, list_sessions, available_models)
    # run synchronously on the GTK main thread and can block for a second
    # or more. We swap the provider pill to a yellow "WORKING …" label
    # + a working class on the whole header BEFORE the call, then force
    # GTK to paint the new state via a MainContext iteration pump so the
    # user sees the change even though the call is blocking.

    _WORKING_SPINNER = "󰦖"  # nf-md-spin; paired with the working label.

    def _set_working(self, label: str = "WORKING") -> None:
        """Flash the header yellow with a spinner + `label`. Paints
        synchronously so callers can invoke this immediately before a
        blocking RPC and the user sees the state change."""
        if not hasattr(self, "_header"):
            return
        self._header.add_css_class("working")
        self._provider_label.set_label(f"{self._WORKING_SPINNER} {label}")
        self._provider_label.add_css_class("working")
        # Pump one round of the GTK main loop so the new state paints
        # before the blocking call starts.
        ctx = GLib.MainContext.default()
        for _ in range(8):
            if not ctx.pending():
                break
            ctx.iteration(False)

    def _clear_working(self) -> None:
        """Revert the working indicator to the normal provider pill."""
        if not hasattr(self, "_header"):
            return
        self._header.remove_css_class("working")
        self._provider_label.remove_css_class("working")
        self._provider_label.set_label(self._header_title())

    def _assistant_title(self) -> str:
        if self._model:
            return self.ASSISTANT_TITLE_WITH_MODEL_FMT.format(
                provider=self._provider_name, model=self._model
            )

        return self.ASSISTANT_TITLE_FMT.format(provider=self._provider_name)

    def _append_chunk(self, chunk: str) -> bool:
        """Main-thread-safe sink for adapter chunks. Appends to the
        currently-streaming assistant card and re-arms the autofollow
        window so the frame tick keeps the viewport locked to the
        bottom across the card's multi-pass reflow. Returns False so
        it composes with `GLib.idle_add`."""
        if self._active_assistant is not None:
            self._active_assistant.append(chunk)
        self._arm_autofollow()

        return False

    def _append_thinking(self, chunk: str) -> bool:
        """Main-thread-safe sink for `ThinkingChunk` events. Routes
        the reasoning text into the active assistant card's
        collapsible thinking section. Returns False so it composes
        with `GLib.idle_add`."""
        if self._active_assistant is not None:
            self._active_assistant.append_thinking(chunk)
        # Thinking sections are collapsible; when the first chunk
        # arrives the card inserts an expander whose natural height
        # depends on several layout passes. Kick the follow-up so we
        # don't end up with the expander peeking off the bottom.
        self._schedule_pinned_follow_up()

        return False

    def _apply_plan(self, items: list) -> bool:
        """Main-thread sink for `PlanChunk` events. Re-renders the
        plan section on the active assistant card AND caches the
        snapshot as `_last_plan` so Ctrl+O can reopen it later, even
        after the turn has finalised."""
        if self._active_assistant is not None:
            self._active_assistant.set_plan(items)
            self._last_plan_card = self._active_assistant
        self._last_plan_items = list(items)
        # Plan widgets can grow / shrink several rows in one update;
        # nudge the follow-up so the viewport re-targets the new
        # bottom once layout settles.
        self._schedule_pinned_follow_up()
        return False

    def _run_turn(
        self,
        user_message: str,
        *,
        attachments: Optional[list[PromptAttachment]] = None,
    ) -> None:
        log.info(
            "run_turn: start wire_len=%d attachments=%d",
            len(user_message),
            len(attachments or []),
        )
        first_text_chunk = True
        try:
            for chunk in self._adapter.turn(user_message, attachments=attachments):
                if not self._alive:
                    return
                if isinstance(chunk, ToolCall):
                    # audit=True → tool bubble on the assistant card.
                    # audit=False → blocking PermissionRow (the ACP
                    # `request_permission` path routes around this
                    # through `set_permission_handler`, so this branch
                    # is a fallback for any non-audit producer).
                    if getattr(chunk, "audit", False):
                        GLib.idle_add(self._on_tool_stream_event, chunk)
                    else:
                        GLib.idle_add(self._on_tool_call, chunk)
                    continue
                if isinstance(chunk, ThinkingChunk):
                    GLib.idle_add(self._append_thinking, chunk.text)
                    continue
                if isinstance(chunk, PlanChunk):
                    GLib.idle_add(self._apply_plan, chunk.items)
                    continue
                if isinstance(chunk, SessionInfoChunk):
                    GLib.idle_add(self._apply_session_info, chunk.title)
                    continue
                if first_text_chunk:
                    GLib.idle_add(self._mark_stream_started)
                    first_text_chunk = False
                GLib.idle_add(self._append_chunk, chunk)
        except Exception as e:
            if not self._alive:
                return
            log.exception("turn failed: %s", e)
            GLib.idle_add(self._append_chunk, f"\n\n*error: {e}*\n")
            GLib.idle_add(self.show_error, self._humanise_error(e))
        finally:
            log.info("run_turn: end streamed=%s", self._stream_started)
            if self._alive:
                GLib.idle_add(self._mark_idle)
                # Adapter's `reconcile()` just ran (inside `.turn()`)
                # and may have pulled a drifted model / mode into its
                # state. Repaint the header on the main thread so mode
                # / model / cwd pills reflect the post-turn truth —
                # without this, an agent-side `current_mode_update`
                # lives in `_mode_state` but never reaches the pill.
                GLib.idle_add(self._refresh_session_label)
                self._verify_session_info()

    def _verify_session_info(self) -> None:
        """Reconcile the UI's session title with the agent's authoritative
        copy after a turn completes.

        Agents SHOULD push `session_info_update` notifications when they
        rename a session — but claude-agent-acp in particular sometimes
        ships a title only at first-turn summary time and then stays
        quiet on subsequent renames. Polling `session/list` once per
        turn catches those drifts. Runs in a worker so the blocking RPC
        doesn't stall the main thread."""

        def _poll() -> None:
            sid = getattr(self._adapter, "session_id", None)
            if not sid:
                return
            try:
                sessions = self._adapter.list_sessions()
            except Exception as e:
                log.debug("verify_session_info: list_sessions raised: %s", e)
                return
            match = next((s for s in sessions if s.get("session_id") == sid), None)
            if not match:
                return
            title = (match.get("title") or "").strip()
            if title and title != self._session_title:
                log.info(
                    "verify_session_info: reconciling title %r -> %r",
                    self._session_title,
                    title,
                )
                GLib.idle_add(self._apply_session_info, title)

        background_tasks.submit("pilot-verify-session", _poll)

    @staticmethod
    def _humanise_error(exc: BaseException) -> str:
        """Strip an ACP exception down to the human-readable line the
        user actually needs. ACP errors come through as
        `acp.RequestError('Internal error: Prompt is too long',
        code=-32603)`; we surface `"Prompt is too long"` + the code."""
        msg = str(exc).strip()
        code = getattr(exc, "code", None)
        if code is not None:
            return f"{msg} (acp {code})"
        return msg or exc.__class__.__name__

    def show_error(self, message: str) -> bool:
        """Surface `message` in the dismissable toast strip. Repeat
        calls replace the text; no stacking (the user cares about the
        latest failure, not a history). Ctrl+E dismisses. Safe to
        call from the GTK main thread — for worker threads, wrap in
        `GLib.idle_add(window.show_error, msg)`."""
        if not hasattr(self, "_toast_label"):
            return False
        self._toast_label.set_label(message)
        self._toast.set_visible(True)
        return False

    def dismiss_error(self) -> None:
        """Hide the error toast and clear its text."""
        if not hasattr(self, "_toast"):
            return
        self._toast.set_visible(False)
        self._toast_label.set_label("")

    def _mark_idle(self) -> bool:
        # Only fire the "response finished" toast when we actually
        # streamed something — a denied / cancelled / error-bailed
        # turn that never produced text shouldn't claim completion.
        streamed = self._stream_started
        # Freeze the assistant card's tool-bubble strip BEFORE we
        # drop the active-assistant ref so still-pending bubbles
        # flip to their terminal state on the right card.
        if self._active_assistant is not None:
            try:
                self._active_assistant.freeze_tool_bubbles(
                    cancelled=self._turn_cancelled
                )
            except Exception as e:
                log.warning("freeze_tool_bubbles raised: %s", e)
            # Fold the thinking + plan expanders so the reply sits
            # above cleanly-collapsed scaffolding. `append()` already
            # auto-collapses thinking mid-stream, but plan snapshots
            # can finalise in-progress and mid-turn thinking bursts
            # can re-open the expander — `collapse_reasoning_sections`
            # normalises both at turn-end regardless of interim state.
            try:
                self._active_assistant.collapse_reasoning_sections()
            except Exception as e:
                log.warning("collapse_reasoning_sections raised: %s", e)
        self._streaming = False
        self._stream_started = False
        self._turn_cancelled = False
        self._active_assistant = None
        if self._alive:
            self._compose.focus()
            self._update_phase()
            # Session-resume flag is finalised by the time the first
            # turn wraps; refresh the breadcrumb so `󰑐 restored` shows
            # up (or stays hidden for a fresh new_session). Model
            # reconciliation in `_AcpConverseAdapter.turn` may have
            # expanded `opus` → `opus[1m]` — this picks that up too.
            self._refresh_session_label()
            if streamed:
                self._notify_finished()

        return False

    def _mark_stream_started(self) -> bool:
        """Main-thread sink: first reply chunk landed, flip pending →
        streaming (unless we're currently awaiting approval / an answer,
        in which case `_update_phase` keeps the blue awaiting pill).
        Also refreshes the session breadcrumb — by now
        `_ensure_started` has run, so `session_resumed` is authoritative
        and the `󰑐 restored` tag can surface."""
        self._stream_started = True
        self._update_phase()
        self._refresh_session_label()

        return False

    # -- Phase colouring -----------------------------------------------

    _PHASE_CLASSES = ("idle", "pending", "streaming", "awaiting")
    # Icon theme names so `notify-send` can pull the right glyph per
    # notification type. `dialog-*` names are stable across Adwaita /
    # Papirus / Breeze; if a theme is missing one the daemon just
    # drops the icon without failing.
    _NOTIFY_ICON_FINISHED = "dialog-information-symbolic"
    _NOTIFY_ICON_APPROVAL = "dialog-password-symbolic"

    def _should_notify(self) -> bool:
        """Only fire desktop toasts when the overlay itself is hidden.
        If it's on screen the provider pill + the new row / banner
        already tell the user what happened — doubling up with a
        notification is just noise. Layer-shell surfaces don't have a
        meaningful `is_active()` (the compositor keeps them focusable
        on-demand only), so visibility alone is the right gate."""
        return not self.get_visible()

    def _notify_title(self, base: str) -> str:
        """Prefix desktop-toast titles with the session suffix in parens
        when one is configured (e.g. `Pilot (plan)`). Makes it obvious
        which of several concurrent pilot overlays raised the toast."""
        if self._session_suffix:
            return f"{base} ({self._session_suffix})"
        return base

    def _notify_finished(self) -> None:
        if not self._should_notify():
            return
        notify(
            self._notify_title("Pilot"),
            "Response finished",
            self._NOTIFY_ICON_FINISHED,
            timeout=3000,
        )

    def _notify_approval(self, tool_name: str) -> None:
        if not self._should_notify():
            return
        label = tool_name or "tool"
        notify(
            self._notify_title("Pilot — approval needed"),
            f"Waiting on approval: {label}",
            self._NOTIFY_ICON_APPROVAL,
            timeout=8000,
        )

    def _update_phase(self) -> bool:
        """Recompute the provider pill's phase from current state.

        Priority order:
        - Any pending permission row OR an active question banner →
          `awaiting` (blue). The user's input is the blocker; the
          stream is either holding an approval envelope or typing
          into the question box, and the yellow/red turn colours
          would misrepresent that.
        - `_stream_started` → `streaming` (yellow). Chunks are
          flowing into the active assistant card.
        - `_streaming` without any chunks yet → `pending` (red).
          Turn is in flight, we're waiting for the first byte.
        - Otherwise → `idle` (green)."""
        if self._permissions:
            phase = "awaiting"
        elif self._stream_started:
            phase = "streaming"
        elif self._streaming:
            phase = "pending"
        else:
            phase = "idle"
        self._phase = phase
        for cls in self._PHASE_CLASSES:
            if cls == phase:
                self._provider_label.add_css_class(cls)
            else:
                self._provider_label.remove_css_class(cls)
        _signal_waybar_safe()

        return False

    # -- Queue ----------------------------------------------------------

    def _enqueue(
        self,
        message: str,
        *,
        display: Optional[str] = None,
        attachments: Optional[list[PromptAttachment]] = None,
    ) -> None:
        row = QueueRow(
            text=message,
            display=display,
            attachments=attachments,
            on_send=self._on_queue_send,
            on_remove=self._on_queue_remove,
            on_edit_commit=self._on_queue_edit,
        )
        self._queue.append(row)
        self._queue_listbox.append(row)
        self._queue_box.set_visible(True)
        _signal_waybar_safe()

    def _pop_queue_front(self) -> Optional[tuple[str, str]]:
        if not self._queue:
            return None
        row = self._queue.pop(0)
        self._queue_listbox.remove(row)
        if not self._queue:
            self._queue_box.set_visible(False)
        _signal_waybar_safe()

        return row.text(), row.display()

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
        # Manual-drain policy: 󰌑 dispatches this specific card only if
        # nothing is currently streaming. While streaming, the button is
        # a soft no-op — the user can wait or use the 󰌑 on another
        # card later. Keeps the conversation's pacing in their hands.
        if self._streaming:
            log.info("ignoring queue-send while streaming")
            return
        wire = row.text()
        display = row.display()
        attachments = row.attachments()
        self._remove_queue_row(row)
        if not wire and not attachments:
            return
        self._start_turn(wire, display=display, attachments=attachments)

    def _send_next_queued(self) -> bool:
        """Dispatch the oldest pending queue row — the Ctrl+󰌑 keybind's
        target. No-op when the queue is empty; reuses the per-row send
        path so the streaming guard behaves identically to clicking the
        row's own 󰌑 button. Returns True when a row was dispatched so
        callers can propagate that as the keyboard-event handled flag."""
        if not self._queue:
            return False
        self._on_queue_send(self._queue[0])
        return True

    def _discard_next_queued(self) -> bool:
        """Drop the oldest pending queue row — the Ctrl+Backspace keybind's
        target. Mirrors clicking the row's own × button but without
        requiring mouse focus, so users can flush a mistyped paste-and-
        enter before it streams. Returns True when a row was removed."""
        if not self._queue:
            return False
        self._remove_queue_row(self._queue[0])
        return True

    def _on_queue_remove(self, row: QueueRow) -> None:
        self._remove_queue_row(row)

    def _on_queue_edit(self, _row: QueueRow, _new_text: str) -> None:
        # Row keeps its slot; the card shows the updated text. Nothing
        # else to do — dispatch_turn reads row.text() on drain / send.
        pass

    # -- Permissions ---------------------------------------------------

    def _on_tool_stream_event(self, call: ToolCall) -> bool:
        """Main-thread sink for `audit=True` `ToolCall` events from
        `adapter.turn()`. Routes to the assistant card's bubble strip:
        first event for a tool_id builds the bubble; follow-ups update
        its status (pending → completed / cancelled). Returns False
        so `GLib.idle_add` fires once."""
        card = self._active_assistant
        if card is None:
            # Turn may have already finalised (late completion arriving
            # after `_mark_idle`). Replay onto the most recent
            # assistant card so audit remains accurate.
            for existing in reversed(self._cards):
                if existing.role == "assistant":
                    card = existing
                    break
        if card is None:
            return False
        try:
            if call.tool_id in card._tool_bubbles:
                card.update_tool_bubble(
                    call.tool_id,
                    status=call.status,
                    arguments=call.arguments,
                )
            else:
                card.append_tool_bubble(call)
        except Exception as e:
            log.warning("tool bubble update raised: %s", e)
        # A fresh bubble grows the assistant card's height; kick the
        # autofollow window so we don't end up peeking at the bubble
        # row from above after it renders.
        self._arm_autofollow()

        return False

    def _on_tool_call(self, call: ToolCall) -> bool:
        """Main-thread sink for `ToolCall` events from `adapter.turn()`.
        Scheduled via `GLib.idle_add` from the worker thread — returns
        False so it fires once and detaches."""
        if call.name and self._permission.decide(call.name, call.kind) is not None:
            log.debug("tool %r short-circuited by permission state", call.name)
            return False
        row = PermissionRow(
            call,
            on_allow=self._on_permission_allow,
            on_trust=self._on_permission_trust,
            on_deny=self._on_permission_deny,
            tool_formatters=self._tool_formatters(),
            on_auto_reject=self._on_permission_auto_reject,
        )
        self._install_permission_row(row)
        return False

    def _install_permission_row(self, row: PermissionRow) -> None:
        """Add a freshly-built `PermissionRow` into the stack, cap its
        body scroller to the overlay height, and focus it when it's
        the sole pending row. Shared between the audit path
        (`_on_tool_call`) and the blocking ACP path
        (`show_permission_for_acp`)."""
        row.apply_height_fraction(self.get_allocated_height())
        was_empty = not self._permissions
        self._permissions.append(row)
        self._permissions_listbox.append(row)
        self._permissions_box.set_visible(True)
        self._update_phase()
        # Only grab focus when this is the first pending row — otherwise
        # we'd yank keyboard focus away from whichever row the user is
        # already answering.
        if was_empty:
            # Deferred so focus lands after GTK has allocated the new
            # widget; grab_focus on a freshly-added button is a no-op.
            GLib.idle_add(row.focus_allow)
        call = row.call
        self._notify_approval(call.name)

    def _remove_permission_row(self, row: PermissionRow) -> None:
        if row not in self._permissions:
            return
        self._permissions.remove(row)
        self._permissions_listbox.remove(row)
        if self._permissions:
            # Pull focus onto the new oldest row so the user can keep
            # tabbing without hunting for the next prompt.
            GLib.idle_add(self._permissions[0].focus_allow)
        else:
            self._permissions_box.set_visible(False)
        self._update_phase()

    def show_permission_for_acp(self, call: ToolCall, options, resolve) -> bool:
        """Render a permission row for an ACP `session/request_permission`
        event and invoke `resolve(option_id: Optional[str])` when the
        user clicks. The agent subprocess is blocked on this round-trip
        — it won't run the tool until we pick an option.

        `resolve(None)` sends a `cancelled` outcome; `resolve("<id>")`
        sends `selected`. The four PermissionRow buttons each map to a
        PermissionOption kind (`allow_once`, `allow_always`,
        `reject_once`, `reject_always`); `select_option_id` finds the
        closest match when the agent didn't ship that exact kind (e.g.
        opencode's `once / always / reject` triad has no `reject_always`).

        Belt-and-suspenders short-circuits: if the tool name is already
        in an auto list, we answer without surfacing a row so the UI
        stays consistent with the pill state."""

        auto_kind = self._permission.decide(call.name or "", call.kind or "")
        if auto_kind is not None:
            resolve(AcpAdapter.select_option_id(options, auto_kind))
            return False

        def on_allow(r: PermissionRow) -> None:
            self._remove_permission_row(r)
            resolve(AcpAdapter.select_option_id(options, "allow_once"))

        def on_trust(r: PermissionRow) -> None:
            tool_name = r.tool_name
            if tool_name:
                self._permission.trust(tool_name)
                self._sync_permission_state()
            for existing in list(self._permissions):
                if existing.tool_name == tool_name:
                    self._remove_permission_row(existing)
            resolve(AcpAdapter.select_option_id(options, "allow_always"))

        def on_deny(r: PermissionRow) -> None:
            self._remove_permission_row(r)
            resolve(AcpAdapter.select_option_id(options, "reject_once"))

        def on_auto_reject(r: PermissionRow) -> None:
            tool_name = r.tool_name or "tool"
            if r.tool_name:
                self._permission.auto_reject(r.tool_name)
                self._sync_permission_state()
            self._turn_cancelled = True
            try:
                self._adapter.cancel()
            except Exception as e:
                log.warning("adapter cancel raised during auto-reject: %s", e)
            if self._active_assistant is not None:
                self._active_assistant.append(
                    f"\n\n*— cancelled (auto-rejected: {tool_name}) —*"
                )
            for existing in list(self._permissions):
                if existing.tool_name == r.tool_name:
                    self._remove_permission_row(existing)
            resolve(AcpAdapter.select_option_id(options, "reject_always"))

        row = PermissionRow(
            call,
            on_allow=on_allow,
            on_trust=on_trust,
            on_deny=on_deny,
            on_auto_reject=on_auto_reject,
            tool_formatters=self._tool_formatters(),
        )
        self._install_permission_row(row)
        return False

    def _on_permission_allow(self, row: PermissionRow) -> None:
        self._remove_permission_row(row)

    def _on_permission_trust(self, row: PermissionRow) -> None:
        name = row.tool_name
        if name:
            self._permission.trust(name)
            self._sync_permission_state()
        # Drop every pending row for the same tool while we're at it —
        # the user just said they trust it, no point keeping duplicate
        # prompts for concurrent calls on screen.
        for existing in list(self._permissions):
            if existing.tool_name == name:
                self._remove_permission_row(existing)

    def _on_permission_auto_reject(self, row: PermissionRow) -> None:
        """Audit-only auto-reject path — mirrors the gated variant in
        `show_permission_for_acp` but without a resolver, because the
        bubbles it fires for already surfaced fire-and-forget."""
        name = row.tool_name or "tool"
        if row.tool_name:
            self._permission.auto_reject(row.tool_name)
            self._sync_permission_state()
        self._turn_cancelled = True
        try:
            self._adapter.cancel()
        except Exception as e:
            log.warning("adapter cancel raised during auto-reject: %s", e)
        if self._active_assistant is not None:
            self._active_assistant.append(
                f"\n\n*— cancelled (auto-rejected: {name}) —*"
            )
        for existing in list(self._permissions):
            if existing.tool_name == row.tool_name:
                self._remove_permission_row(existing)

    def attach_session(self, session: Session) -> None:
        """Wire a live Session onto the window. The window stays
        functional without one — every mutation is local-only, the
        session handle just lets us signal waybar when state changes."""
        self._session = session
        self._sync_permission_state()

    def _sync_permission_state(self) -> None:
        """Nudge the permissions palette so it reflects the latest
        trust / auto-approve / auto-reject sets. If the palette isn't
        open, the next open() pulls fresh entries automatically."""
        if (
            self._permissions_palette is not None
            and self._permissions_palette.is_open()
        ):
            self._permissions_palette.open(self._collect_permission_entries())

    def _on_permission_deny(self, row: PermissionRow) -> None:
        """Cancel the in-flight stream and revoke trust for this tool.
        Server-side tools have usually run by the time we see the event
        (we're reading the record, not predicting it); this is a
        best-effort cancel of the remaining response plus a trust
        revoke so the next turn can't re-trigger the same tool."""
        tool_name = row.tool_name or "tool"
        self._turn_cancelled = True
        try:
            self._adapter.cancel()
        except Exception as e:
            log.warning("adapter cancel raised during deny: %s", e)
        if self._active_assistant is not None:
            self._active_assistant.append(f"\n\n*— cancelled (denied: {tool_name}) —*")
        self._permission.discard(tool_name)
        self._sync_permission_state()
        for existing in list(self._permissions):
            self._remove_permission_row(existing)

    # -- Scroll / keys / links -----------------------------------------

    _PIN_THRESHOLD_PX = 24
    # Default time horizon (seconds) the tick callback keeps pinning
    # to the bottom after an `_arm_autofollow()` call. Each new
    # content event extends the deadline — so a chatty turn that
    # streams chunks + tool bubbles + thinking over several seconds
    # keeps the scroll locked throughout. 0.8s is long enough to
    # outlast the slowest markdown re-layout I've seen, short enough
    # that an idle window stops ticking promptly.
    _AUTOFOLLOW_WINDOW_S = 0.8

    def _on_vadj_value_changed(self, adj) -> None:
        """Update the pinned flag based on where the user actually sat
        down in the scrollable. Programmatic scrolls (from our own
        `set_value`) are ignored via `_programmatic_scroll`, otherwise
        they'd race the reflow loop and keep unpinning us."""
        if self._programmatic_scroll:
            return
        bottom = max(0.0, adj.get_upper() - adj.get_page_size())
        if adj.get_value() >= bottom - self._PIN_THRESHOLD_PX:
            self._pinned = True
        else:
            self._pinned = False

    def _on_vadj_upper_changed(self, _adj, _pspec) -> None:
        """Content grew OR the viewport shrank. Either way, arm the
        autofollow window so the frame tick keeps the scrollbar glued
        to the new bottom across every subsequent re-measure. The
        adjustment param is unused — `_arm_autofollow` pulls the
        current adj from the scroller directly."""
        if not self._pinned:
            return
        self._arm_autofollow()

    def _arm_autofollow(self, window_s: Optional[float] = None) -> None:
        """Enter (or extend) the autofollow window. While active, a GDK
        frame-tick callback re-pins the scrollbar to the bottom every
        vblank — so we can't lose races against markdown cards that
        re-measure across multiple ticks, tool bubbles expanding, or
        thinking / plan expanders opening. The deadline advances with
        every call, so a continuous stream of content events keeps the
        window alive; it only closes once data actually stops arriving.

        `window_s` overrides the default horizon for one-shot kick
        events (e.g. `_force_scroll_to_bottom` wants a longer window
        on first-turn card appends)."""
        if not self._pinned:
            return
        horizon = window_s if window_s is not None else self._AUTOFOLLOW_WINDOW_S
        self._autofollow_deadline = time.monotonic() + horizon
        if self._autofollow_tick_id is not None:
            return
        self._autofollow_tick_id = self._conv_scroller.add_tick_callback(
            self._autofollow_tick
        )

    def _autofollow_tick(self, _widget, _frame_clock) -> bool:
        """Per-frame callback. Returns True (GDK_SOURCE_CONTINUE) while
        pinned + within window; returns False to deregister when the
        user scrolls up or the window expires.

        Work inside the tick is trivial when already at the bottom —
        just a comparison + early out — so leaving it running for a
        few extra frames after content stops is effectively free."""
        if not self._pinned or time.monotonic() >= self._autofollow_deadline:
            self._autofollow_tick_id = None
            return False
        adj = self._conv_scroller.get_vadjustment()
        target = max(0.0, adj.get_upper() - adj.get_page_size())
        if abs(adj.get_value() - target) > 0.5:
            self._programmatic_scroll = True
            try:
                adj.set_value(target)
            finally:
                self._programmatic_scroll = False
        return True

    def _schedule_pinned_follow_up(self) -> None:
        """Back-compat shim. Every caller that used to schedule an idle
        + 60ms retry now just re-arms the frame-tick autofollow
        window; the tick runs on every vblank until content settles,
        which subsumes whatever the idle retries were trying to do."""
        self._arm_autofollow()

    def _force_scroll_to_bottom(self) -> None:
        """Hard pin — used when we KNOW the user wants the view at the
        bottom (send button clicked, new card appended). Sets `_pinned`
        explicitly (so a user who had scrolled up still jumps), then
        arms a generous autofollow window so the multi-pass layout of
        a freshly-mounted TurnCard (markdown measurements, wrapping,
        image loads) can't leave us above the bottom line."""
        self._pinned = True
        adj = self._conv_scroller.get_vadjustment()
        self._programmatic_scroll = True
        try:
            adj.set_value(max(0.0, adj.get_upper() - adj.get_page_size()))
        finally:
            self._programmatic_scroll = False
        self._arm_autofollow(window_s=1.2)

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
        palettes = {
            "resource": self._palette,
            "permissions": self._permissions_palette,
            "mcp": self._mcp_palette,
            "models": self._models_palette,
            "modes": self._modes_palette,
            "sessions": self._sessions_palette,
            "cwd": self._cwd_palette,
            "root": self._root_palette,
            "keybindings": self._keybindings_palette,
        }
        opens = {name: p is not None and p.is_open() for name, p in palettes.items()}
        any_palette_open = any(opens.values())
        if ctrl and keyval == Gdk.KEY_space:
            # Single entry point for panel-style actions. Closes any
            # open palette (root or leaf) so Ctrl+Space also acts as
            # "escape the palette stack"; otherwise opens the root.
            if any_palette_open:
                for name, p in palettes.items():
                    if opens[name] and p is not None:
                        p.close()
            else:
                self._open_root_palette()
            return True
        if ctrl and keyval == Gdk.KEY_k:
            # Ctrl+K is the keybindings palette — flat list of every
            # binding; Enter triggers the chosen action. Permissions
            # moved into the root Ctrl+Space dispatcher.
            if opens["keybindings"] and self._keybindings_palette is not None:
                self._keybindings_palette.close()
            else:
                self._open_keybindings_palette()
            return True
        if ctrl and keyval == Gdk.KEY_e:
            self.dismiss_error()
            return True
        if any_palette_open and keyval == Gdk.KEY_Escape:
            return False
        shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        if ctrl and shift and keyval in (Gdk.KEY_q, Gdk.KEY_Q):
            # Ctrl+Shift+Q: hard close (tears the ACP session + the
            # pilot process down). Requires Shift on top of Ctrl so
            # a muscle-memory Ctrl+Q doesn't nuke the session by
            # accident — the common "oops I meant Ctrl+W" typo is
            # now a hide instead of a full teardown.
            self.close()
            return True
        if ctrl and keyval in (Gdk.KEY_q, Gdk.KEY_w):
            # Ctrl+Q and Ctrl+W both hide the overlay without
            # tearing the session down. Ctrl+Q is the conventional
            # app-close shortcut so we honour muscle memory, while
            # Ctrl+Shift+Q is reserved for the hard teardown above.
            # Ctrl+W stays as an alternative for users who still
            # think of pilot as a "tab" to close.
            self.set_visible(False)
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
        if ctrl and keyval == Gdk.KEY_y:
            self._yank_last_assistant()
            return True
        if ctrl and keyval == Gdk.KEY_g:
            self._accept_first_permission()
            return True
        if ctrl and keyval == Gdk.KEY_r:
            self._reject_first_permission()
            return True
        if ctrl and keyval == Gdk.KEY_t:
            self._toggle_last_thinking()
            return True
        if ctrl and keyval == Gdk.KEY_o:
            self._open_last_plan()
            return True
        # Ctrl+󰌑 dispatches the next queued turn. Declared AFTER the
        # other ctrl-combos so a focused compose TextView (which
        # treats plain 󰌑 as "submit") still gets first crack at the
        # naked Return key; only the ctrl-modified form comes here.
        if ctrl and keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if self._send_next_queued():
                return True
        # Ctrl+Backspace discards the head of the queue — pairs with
        # Ctrl+󰌑 (send next) so both keyboard-only queue actions live
        # on the same modifier set.
        if ctrl and keyval == Gdk.KEY_BackSpace:
            if self._discard_next_queued():
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
        # Escape falls through untouched — GTK's default handlers on
        # focused widgets (TextView clears its selection, Entry
        # discards preedit, etc.) get to fire. Toggling the overlay
        # off moved to `Ctrl+W`.

        return False

    def _scroll_to(self, fraction: float) -> None:
        """Jump to `fraction` of the scroll range. 0.0 = top, 1.0 = bottom.
        Updates `_pinned` so the auto-follow state matches the landing
        position."""
        adj = self._conv_scroller.get_vadjustment()
        bottom = max(0.0, adj.get_upper() - adj.get_page_size())
        target = adj.get_lower() + (bottom - adj.get_lower()) * max(
            0.0, min(1.0, fraction)
        )
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
        self._turn_cancelled = True
        try:
            self._adapter.cancel()
        except Exception as e:
            log.warning("adapter cancel raised: %s", e)
        if self._active_assistant is not None:
            self._active_assistant.append("\n\n*— cancelled —*")

    # MIME prefixes that Ctrl+P captures as attachments instead of text.
    # Today just images (CodeCompanion's `ResourceResponse:image(data,
    # mime)` equivalent); extend here when audio / arbitrary blobs are
    # worth surfacing as pills.
    _ATTACHMENT_MIME_PREFIXES: tuple[str, ...] = ("image/",)

    def _paste_clipboard_into_compose(self) -> None:
        """Ctrl+P: inspect the clipboard and route into the right sink.
        Image / binary payloads become attachment pills so they ride on
        the next turn as ACP content blocks; text falls through to the
        compose TextView."""
        mimes = InputAdapterClipboard.list_mime_types()
        log.info("paste: clipboard mimes=%s", mimes)
        chosen_mime = self._pick_attachment_mime(mimes)
        if chosen_mime is not None:
            data = InputAdapterClipboard.read_binary(chosen_mime)
            if data:
                log.info("paste: attaching %s (%d bytes)", chosen_mime, len(data))
                self._pending_attachments.append(
                    PromptAttachment(mime_type=chosen_mime, data=data)
                )
                self._refresh_attachment_pills()
                self._compose.focus()
                return
            log.warning(
                "clipboard advertised %s but wl-paste returned nothing; "
                "falling back to text",
                chosen_mime,
            )
        text = InputAdapterClipboard().read() or ""
        log.info("paste: text fallback len=%d", len(text))
        if not text:
            return
        self._compose.focus()
        self._compose.append_text(text)

    def _pick_attachment_mime(self, mimes: list[str]) -> Optional[str]:
        for mime in mimes:
            for prefix in self._ATTACHMENT_MIME_PREFIXES:
                if mime.startswith(prefix):
                    return mime
        return None

    def _refresh_attachment_pills(self) -> None:
        self._compose.set_attachment_pills(
            [
                (self._attachment_label(att), att.mime_type, att)
                for att in self._pending_attachments
            ],
            on_remove=self._on_pending_attachment_remove,
        )

    @staticmethod
    def _attachment_label(att: PromptAttachment) -> str:
        """Short human label for the pill. Images say `󰁨 image/png` with
        the byte size so the user knows what they're about to send."""
        mime = att.mime_type or "blob"
        if att.data is not None:
            size = len(att.data)
            if size >= 1024 * 1024:
                size_str = f"{size / (1024 * 1024):.1f}MB"
            elif size >= 1024:
                size_str = f"{size / 1024:.1f}KB"
            else:
                size_str = f"{size}B"
            return f"󰁨 {mime} · {size_str}"
        if att.uri:
            return f"󰁨 {mime} · {att.uri}"
        return f"󰁨 {mime}"

    def _on_pending_attachment_remove(self, key: object) -> None:
        self._pending_attachments = [
            att for att in self._pending_attachments if att is not key
        ]
        self._refresh_attachment_pills()

    def _open_root_palette(self) -> None:
        """Ctrl+Space entry point. A select-mode palette listing the
        three leaf palettes — Enter on one closes the root and opens
        that leaf. Kept minimal: no fuzzy search value beyond the
        three options, no preseed, no delete hook. Functions purely
        as a keyboard-navigable dispatcher."""
        if self._root_palette is None:
            self._root_palette = CommandPalette(
                host_overlay=self._compose_overlay,
                on_commit=self._commit_root_palette,
                on_cancel=self._compose.focus,
                select_mode=True,
                placeholder="Command palette — Enter opens · Esc cancels",
            )
        self._size_palette(self._root_palette)
        self._root_palette.preseed_active(set())
        self._root_palette.open(
            [
                (
                    "skills",
                    "Skills",
                    "attach skills as resources on the next turn",
                    "skills",
                ),
                (
                    "references",
                    "References",
                    "attach shared references as resources on the next turn",
                    "references",
                ),
                ("mcps", "MCPs", "toggle MCP servers for this session", "mcps"),
                ("models", "Models", "switch the agent's active model", "models"),
                ("modes", "Modes", "switch the agent's session mode", "modes"),
                (
                    "commands",
                    "Slash Commands",
                    "run one of the agent's advertised slash commands",
                    "commands",
                ),
                (
                    "cwd",
                    "Current Working Directory",
                    "browse + pick a new working directory",
                    "cwd",
                ),
                (
                    "permissions",
                    "Permissions",
                    "review / revoke trusted tools",
                    "permissions",
                ),
                (
                    "sessions",
                    "Sessions",
                    "restore a previous session or start fresh",
                    "sessions",
                ),
            ]
        )

    def _commit_root_palette(self, entries) -> None:
        """Root-palette commit handler. Opens the chosen leaf palette
        on the next idle tick (the root has already detached by the
        time `on_commit` fires, so raising the child directly is
        safe). Unknown kinds just refocus the compose."""
        if not entries:
            self._compose.focus()
            return
        kind = entries[0][0]
        dispatch = {
            "skills": self._open_resource_palette,
            "references": self._open_references_palette,
            "mcps": self._open_mcp_palette,
            "models": self._open_models_palette,
            "modes": self._open_modes_palette,
            "commands": self._open_commands_palette,
            "cwd": self._open_cwd_palette,
            "permissions": self._open_permissions_palette,
            "sessions": self._open_sessions_palette,
        }
        handler = dispatch.get(kind, self._compose.focus)
        handler()

    def _open_resource_palette(self) -> None:
        """Raise the resource (skills) palette over the compose area
        with a freshly-collected resource list. Reached via the root
        palette's `Skills` row. Lazy-constructs the widget on first
        call, re-uses it on subsequent opens — state (search input,
        active toggles) resets every `open()` call so stale ticks
        don't leak across sessions.

        `CommandPalette` from `lib.overlay` handles the list + key
        wiring; `_preseed_resource_active_from_compose` and
        `_commit_resources_to_compose` are the two pilot-specific
        shims that translate between its generic tuple interface and
        our `#{kind/name}` compose-buffer token format."""
        if self._palette is None:
            self._palette = CommandPalette(
                host_overlay=self._compose_overlay,
                on_commit=self._commit_resources_as_pills,
                on_cancel=self._compose.focus,
                placeholder=(
                    "Search skills — Tab ticks · Enter attaches · Esc cancels"
                ),
            )
        self._size_palette(self._palette)
        resources = self._collect_resources()
        # Preseed from the already-attached pills so re-opening the
        # palette shows which resources are currently queued.
        self._palette.preseed_active({(k, n) for (k, n, _d) in self._pending_resources})
        self._palette.open(resources)

    def _open_references_palette(self) -> None:
        """Raise the references palette over the compose area. Sibling
        of `_open_resource_palette`, wired to `_collect_references`
        (filters the MCP `resources/list` for `reference/*`) and
        reusing `_commit_resources_as_pills` so picked references land
        in the same `_pending_resources` list skills go through — the
        pending-turn expansion path handles `kind == "reference"` via
        `read_reference` already, no separate commit pipeline needed."""
        if self._references_palette is None:
            self._references_palette = CommandPalette(
                host_overlay=self._compose_overlay,
                on_commit=self._commit_resources_as_pills,
                on_cancel=self._compose.focus,
                placeholder=(
                    "Search references — Tab ticks · Enter attaches · Esc cancels"
                ),
            )
        self._size_palette(self._references_palette)
        entries = self._collect_references()
        self._references_palette.preseed_active(
            {(k, n) for (k, n, _d) in self._pending_resources}
        )
        self._references_palette.open(entries)

    def _commit_resources_as_pills(
        self, active_entries: list[tuple[str, str, str, str]]
    ) -> None:
        """Palette commit handler — stores picked resources in the
        window's `_pending_resources` list and refreshes the compose-
        bar pill strip. No tokens get inserted into the compose text;
        the expansion happens inside `dispatch_turn` right before the
        message is handed to the adapter."""
        self._pending_resources = [(k, n, d) for (k, n, d, _p) in active_entries]
        self._refresh_resource_pills()
        self._compose.focus()

    def _refresh_resource_pills(self) -> None:
        self._compose.set_resource_pills(
            [(k, n, d) for (k, n, d) in self._pending_resources],
            on_remove=self._on_pending_resource_remove,
        )

    def _on_pending_resource_remove(self, kind: str, name: str) -> None:
        self._pending_resources = [
            (k, n, d)
            for (k, n, d) in self._pending_resources
            if not (k == kind and n == name)
        ]
        self._refresh_resource_pills()

    def _open_permissions_palette(self) -> None:
        """Reached via Ctrl+Space → Permissions. Lists every trusted
        / auto-approved / auto-rejected tool; Tab ticks rows, Enter
        drops each ticked tool from its bucket. The compose-bar pill
        strip overflows past a dozen entries — a filterable list
        scales better and keeps the compose area clear."""
        if self._permissions_palette is None:
            self._permissions_palette = CommandPalette(
                host_overlay=self._compose_overlay,
                on_commit=self._commit_permission_removal,
                on_cancel=self._compose.focus,
                placeholder=(
                    "Search permissions — Tab toggles · Enter drops · Esc cancels"
                ),
            )
        self._size_palette(self._permissions_palette)
        self._permissions_palette.preseed_active(set())
        self._permissions_palette.open(self._collect_permission_entries())

    # ── Keybindings palette (Ctrl+K) ─────────────────────────────
    #
    # Flat list of every window-level binding. Select-mode — Enter
    # fires the chosen action on the next idle tick (palette has
    # already detached by the time the callback runs, so raising
    # another palette directly is safe).
    #
    # Entries are `(kind, key-label, action-description, action-id)`.
    # `action-id` maps onto `_KEYBINDING_ACTIONS` below for dispatch.

    _KEYBINDINGS: tuple[tuple[str, str, str], ...] = (
        (
            "Ctrl+Space",
            "open the command palette (skills · mcps · models · permissions · sessions)",
            "root_palette",
        ),
        ("Ctrl+K", "show this keybindings list", "keybindings"),
        ("Ctrl+E", "dismiss the error toast", "dismiss_error"),
        ("Ctrl+F", "focus the compose box", "focus_compose"),
        ("Ctrl+P", "paste clipboard into compose (image-aware)", "paste_clipboard"),
        ("Ctrl+D", "cancel the current turn", "cancel_turn"),
        ("Ctrl+Y", "yank the last assistant message", "yank_assistant"),
        ("Ctrl+G", "accept the first pending permission", "accept_permission"),
        ("Ctrl+R", "reject the first pending permission", "reject_permission"),
        ("Ctrl+T", "toggle the last thinking expander", "toggle_thinking"),
        ("Ctrl+O", "reopen the last plan card", "open_plan"),
        ("Ctrl+󰌑", "send the next queued turn", "send_next_queued"),
        ("Ctrl+⌫", "discard the next queued turn", "discard_next_queued"),
        ("Ctrl+Shift+Q", "close pilot (tears the session down)", "close"),
        ("Ctrl+Q", "hide the overlay (session stays alive)", "hide"),
        ("Ctrl+W", "hide the overlay (alias for Ctrl+Q)", "hide"),
        ("Home", "scroll to top of the conversation", "scroll_top"),
        ("End", "scroll to bottom of the conversation", "scroll_bottom"),
        ("PgUp", "scroll the conversation up one page", "scroll_page_up"),
        ("PgDn", "scroll the conversation down one page", "scroll_page_down"),
        ("Esc", "clear widget selection / close open palette", "noop"),
    )

    def _open_keybindings_palette(self) -> None:
        """Raise the keybindings palette (Ctrl+K). Single-select —
        Enter fires the chosen action."""
        if self._keybindings_palette is None:
            self._keybindings_palette = CommandPalette(
                host_overlay=self._compose_overlay,
                on_commit=self._commit_keybinding_choice,
                on_cancel=self._compose.focus,
                select_mode=True,
                placeholder="Keybindings — Enter runs · Esc cancels",
            )
        self._size_palette(self._keybindings_palette)
        self._keybindings_palette.preseed_active(set())
        self._keybindings_palette.open(
            [("key", key, desc, action) for key, desc, action in self._KEYBINDINGS]
        )

    def _commit_keybinding_choice(self, entries) -> None:
        """Dispatch the picked keybinding to its handler. The dispatch
        table is built lazily per-call so it binds to this instance's
        current methods (not a class-level snapshot that might drift
        if a subclass overrides one)."""
        if not entries:
            self._compose.focus()
            return
        action = entries[0][3]
        dispatch = {
            "root_palette": self._open_root_palette,
            "keybindings": self._open_keybindings_palette,
            "dismiss_error": self.dismiss_error,
            "focus_compose": self._compose.focus,
            "paste_clipboard": self._paste_clipboard_into_compose,
            "cancel_turn": self._cancel_current_turn,
            "yank_assistant": self._yank_last_assistant,
            "accept_permission": self._accept_first_permission,
            "reject_permission": self._reject_first_permission,
            "toggle_thinking": self._toggle_last_thinking,
            "open_plan": self._open_last_plan,
            "send_next_queued": self._send_next_queued,
            "discard_next_queued": self._discard_next_queued,
            "close": self.close,
            "scroll_top": lambda: self._scroll_to(0.0),
            "scroll_bottom": lambda: self._scroll_to(1.0),
            "scroll_page_up": lambda: self._scroll_page(-1),
            "scroll_page_down": lambda: self._scroll_page(1),
            "hide": lambda: self.set_visible(False),
            # `Esc` no longer routes through the window's key handler
            # (it's allowed to fall through to focused children so
            # TextView selection-clear et al. keep working); the
            # palette entry is informational only.
            "noop": lambda: None,
        }
        handler = dispatch.get(action)
        if handler is None:
            log.warning("keybindings palette: unknown action %r", action)
            self._compose.focus()
            return
        try:
            handler()
        except Exception as e:
            log.error("keybinding action %s raised: %s", action, e)
            self.show_error(f"Action {action} failed: {e}")

    def _size_palette(self, palette) -> None:
        """Size the floating palette panel to roughly 70% width × 60%
        height of the current sidebar, then let `CommandPalette` force
        those dimensions via `set_size_request`. Falls back to the
        palette's own defaults when the window hasn't laid out yet —
        happens the first time Ctrl+Space fires before the window has
        painted a frame."""
        height = self.get_allocated_height()
        width = self.get_allocated_width()
        if height <= 0 or width <= 0:
            return
        palette.set_size(int(width * 0.9), int(height * 0.6))

    def _commit_permission_removal(self, entries) -> None:
        """Drop every ticked entry from its corresponding permission
        set. Called by the Ctrl+K palette on Enter; `entries` is the
        active-selection list the palette hands back."""
        for _kind, name, _desc, _preview in entries:
            self._permission.discard(name)
        self._sync_permission_state()
        self._compose.focus()

    def _open_mcp_palette(self) -> None:
        """MCP palette (reached via the root palette's `MCPs` row).
        View-only list of every MCP server the
        adapter's ACP session attached. Commit is a no-op — this is
        a cheat-sheet for "what can the agent actually call right
        now", not an editor."""
        if self._mcp_palette is None:
            self._mcp_palette = CommandPalette(
                host_overlay=self._compose_overlay,
                on_commit=lambda _entries: self._compose.focus(),
                on_cancel=self._compose.focus,
                placeholder="Active MCP servers — Esc closes",
            )
        self._size_palette(self._mcp_palette)
        self._mcp_palette.preseed_active(set())
        self._mcp_palette.open(self._collect_mcp_entries())

    def _collect_mcp_entries(self) -> list[tuple[str, str, str, str]]:
        """List every MCP server bound to the active ACP session.
        `McpServerStdio` vs `HttpMcpServer` vs `SseMcpServer` gets
        tagged via the `kind` column so the palette colouring signals
        transport at a glance."""
        out: list[tuple[str, str, str, str]] = []
        session = getattr(self._adapter, "_session", None)
        servers = getattr(session, "mcp_servers", None) or []
        for s in servers:
            transport = type(s).__name__
            if transport == "McpServerStdio":
                desc = f"stdio · {s.command}"
                kind = "mcp_stdio"
            elif transport == "HttpMcpServer":
                desc = f"http · {s.url}"
                kind = "mcp_http"
            elif transport == "SseMcpServer":
                desc = f"sse · {s.url}"
                kind = "mcp_sse"
            else:
                desc = transport
                kind = "mcp"
            out.append((kind, s.name, desc, s.name))
        return out

    def _open_sessions_palette(self) -> None:
        """Sessions palette (reached via the root palette's
        `Sessions` row). Lists every ACP session the agent is willing
        to resume, plus a `new session` sentinel. Select-mode — Enter
        restores the highlighted row, wipes the transcript, and
        queues a `load_session` for the next turn."""
        if self._sessions_palette is None:
            self._sessions_palette = CommandPalette(
                host_overlay=self._compose_overlay,
                on_commit=self._commit_session_restore,
                on_cancel=self._compose.focus,
                select_mode=True,
                placeholder="Restore session — Enter loads · Esc cancels",
            )
        self._size_palette(self._sessions_palette)
        self._sessions_palette.preseed_active(set())
        self._sessions_palette.open(self._collect_session_entries())

    def _collect_session_entries(self) -> list[tuple[str, str, str, str]]:
        """Build the (kind, name, description, preview) list for the
        sessions palette.

        Row shapes:
          - `("new-session", "new session", "…", "new")` — always
            first so the primary escape hatch is one Enter away.
          - `("session", <title-or-short-id>, "<cwd> · <updatedAt>",
            <session_id>)` — one per entry returned by the adapter's
            `list_sessions`. The current session's description gets
            a leading `current ·` marker so the user can tell which
            one they're already on.

        Falls back gracefully: if the adapter can't enumerate, the
        palette still has the `new session` sentinel."""
        out: list[tuple[str, str, str, str]] = [
            ("new-session", "new session", "start a fresh ACP session", "new"),
        ]
        current = getattr(self._adapter, "session_id", None)
        self._set_working("LISTING SESSIONS")
        try:
            sessions = self._adapter.list_sessions()
        except Exception as e:
            log.warning("list_sessions raised: %s", e)
            sessions = []
        finally:
            self._clear_working()
        for s in sessions:
            sid = s.get("session_id", "") or ""
            if not sid:
                continue
            title = (s.get("title") or "").strip() or sid[:12]
            desc_bits: list[str] = []
            if sid == current:
                desc_bits.append("current")
            cwd = (s.get("cwd") or "").strip()
            if cwd:
                desc_bits.append(cwd)
            updated = (s.get("updated_at") or "").strip()
            if updated:
                desc_bits.append(updated)
            desc = " · ".join(desc_bits) or sid
            out.append(("session", title, desc, sid))
        return out

    def _commit_session_restore(self, entries) -> None:
        """Select-mode commit handler: `entries` is a one-element
        list (or empty if the list had no rows). `new-session`
        sentinel hard-resets via `start_fresh_session`; any `session`
        row calls `_restore_session(<id>)` to load it."""
        if not entries:
            self._compose.focus()
            return
        kind, _name, _desc, preview = entries[0]
        if kind == "new-session":
            self.start_fresh_session()
        elif kind == "session" and preview:
            self._restore_session(preview)
        self._compose.focus()

    def _open_models_palette(self) -> None:
        """Models palette (reached via the root palette's `Models`
        row). Select-mode list of the models the agent exposes for
        this session; Enter calls `adapter.set_model`. Empty list
        when the agent doesn't ship a `SessionModelState` (ACP still
        marks the block unstable) — rendered as a single `no models`
        sentinel so the user gets feedback instead of an empty panel."""
        if self._models_palette is None:
            self._models_palette = CommandPalette(
                host_overlay=self._compose_overlay,
                on_commit=self._commit_model_choice,
                on_cancel=self._compose.focus,
                select_mode=True,
                placeholder="Switch model — Enter selects · Esc cancels",
            )
        self._size_palette(self._models_palette)
        self._models_palette.preseed_active(set())
        self._models_palette.open(self._collect_model_entries())

    def _collect_model_entries(self) -> list[tuple[str, str, str, str]]:
        models = []
        self._set_working("LOADING MODELS")
        try:
            models = self._adapter.available_models
        except Exception as e:
            log.warning("available_models raised: %s", e)
        finally:
            self._clear_working()
        if not models:
            return [("empty", "no models", "agent did not expose a model list", "")]
        current = getattr(self._adapter, "current_model_id", None)
        out: list[tuple[str, str, str, str]] = []
        for m in models:
            tag = "current · " if m.model_id == current else ""
            desc = f"{tag}{m.description}" if m.description else (tag + m.model_id)
            out.append(("model", m.name or m.model_id, desc, m.model_id))
        return out

    def _commit_model_choice(self, entries) -> None:
        """Fire ACP `session/set_session_model` so the agent swaps
        models on the existing session. The agent handles context
        trimming / fallback itself — if the new model's window is too
        small, it surfaces the error through the normal turn-error
        path (which the toast picks up)."""
        if not entries:
            self._compose.focus()
            return
        kind, _name, _desc, model_id = entries[0]
        if kind != "model" or not model_id:
            self._compose.focus()
            return
        ok = False
        self._set_working(f"SWITCHING → {model_id}")
        try:
            ok = self._adapter.set_model(model_id)
        except Exception as e:
            log.error("set_model raised: %s", e)
            self.show_error(f"Model switch failed: {e}")
        finally:
            self._clear_working()
        if ok:
            log.info("switched model to %s", model_id)
            self._refresh_session_label()
        else:
            self.show_error(f"Model switch to {model_id} rejected by agent")
        self._compose.focus()

    def _open_modes_palette(self) -> None:
        """Modes palette (reached via the root palette's `Modes` row).
        Select-mode list of the session-modes the agent ships with;
        Enter calls `adapter.set_mode`. Empty list drops a `no modes`
        sentinel row so the user gets feedback when the agent doesn't
        expose any."""
        if self._modes_palette is None:
            self._modes_palette = CommandPalette(
                host_overlay=self._compose_overlay,
                on_commit=self._commit_mode_choice,
                on_cancel=self._compose.focus,
                select_mode=True,
                placeholder="Switch mode — Enter selects · Esc cancels",
            )
        self._size_palette(self._modes_palette)
        self._modes_palette.preseed_active(set())
        self._modes_palette.open(self._collect_mode_entries())

    def _collect_mode_entries(self) -> list[tuple[str, str, str, str]]:
        modes = []
        self._set_working("LOADING MODES")
        try:
            modes = self._adapter.available_modes
        except Exception as e:
            log.warning("available_modes raised: %s", e)
        finally:
            self._clear_working()
        if not modes:
            return [("empty", "no modes", "agent did not expose a mode list", "")]
        current = getattr(self._adapter, "current_mode_id", None)
        out: list[tuple[str, str, str, str]] = []
        for m in modes:
            tag = "current · " if m.mode_id == current else ""
            desc = f"{tag}{m.description}" if m.description else (tag + m.mode_id)
            out.append(("mode", m.name or m.mode_id, desc, m.mode_id))
        return out

    def _commit_mode_choice(self, entries) -> None:
        """Fire ACP `session/set_session_mode` so the agent swaps its
        working mode. Mirrors `_commit_model_choice`."""
        if not entries:
            self._compose.focus()
            return
        kind, _name, _desc, mode_id = entries[0]
        if kind != "mode" or not mode_id:
            self._compose.focus()
            return
        ok = False
        self._set_working(f"SWITCHING → {mode_id}")
        try:
            ok = self._adapter.set_mode(mode_id)
        except Exception as e:
            log.error("set_mode raised: %s", e)
            self.show_error(f"Mode switch failed: {e}")
        finally:
            self._clear_working()
        if ok:
            log.info("switched mode to %s", mode_id)
            self._refresh_session_label()
        else:
            self.show_error(f"Mode switch to {mode_id} rejected by agent")
        self._compose.focus()

    # ── Slash-commands palette ───────────────────────────────────
    #
    # Commands come off the ACP `available_commands_update` notification.
    # Per the ACP slash-commands spec, the client invokes them by
    # sending a regular prompt text starting with `/<name> [args]`;
    # the agent parses the prefix server-side. No separate RPC.
    #
    # Commit flow: stage `/<name> ` (plus a trailing space when the
    # command declares an `input.hint`, so the user's next keystrokes
    # naturally become the argument) into the compose box and focus
    # it. That matches pilot's "external input is staged, user
    # presses Enter to send" invariant, and lets users edit / add
    # args / abandon the command before firing it.

    def _open_commands_palette(self) -> None:
        """Slash-commands palette (reached via root palette → Slash
        Commands). Select-mode list built from the adapter's current
        `available_commands` snapshot. Empty list drops a sentinel
        row so the user gets feedback when the agent hasn't
        advertised any commands (common on agents that only push the
        list after the first turn)."""
        if self._commands_palette is None:
            self._commands_palette = CommandPalette(
                host_overlay=self._compose_overlay,
                on_commit=self._commit_command_choice,
                on_cancel=self._compose.focus,
                select_mode=True,
                placeholder="Run slash command — Enter stages · Esc cancels",
            )
        self._size_palette(self._commands_palette)
        self._commands_palette.preseed_active(set())
        self._commands_palette.open(self._collect_command_entries())

    def _collect_command_entries(self) -> list[tuple[str, str, str, str]]:
        """Build `(kind, name, description, hint)` rows for the
        commands palette. `hint` is stashed in the preview slot so
        the commit handler knows whether to stage a trailing space
        for user input."""
        commands: list = []
        try:
            commands = self._adapter.available_commands
        except Exception as e:
            log.warning("available_commands raised: %s", e)
        if not commands:
            return [
                (
                    "empty",
                    "no commands",
                    "agent has not advertised any slash commands yet",
                    "",
                )
            ]
        out: list[tuple[str, str, str, str]] = []
        for cmd in commands:
            label = f"/{cmd.name}"
            desc = cmd.description or ""
            if cmd.hint:
                # Append the input hint so the palette preview reads
                # e.g. "Search the web for information · query to
                # search for".
                desc = f"{desc} · {cmd.hint}" if desc else cmd.hint
            out.append(("command", label, desc, cmd.hint or ""))
        return out

    def _commit_command_choice(self, entries) -> None:
        """Stage the picked slash command into the compose box so the
        user can add arguments (if the command takes them) and press
        Enter to dispatch. Never auto-sends — matches pilot's
        invariant that external input always waits for confirmation."""
        if not entries:
            self._compose.focus()
            return
        kind, label, _desc, hint = entries[0]
        if kind != "command" or not label:
            self._compose.focus()
            return
        # `label` is already `/name` from `_collect_command_entries`.
        # Append a trailing space for commands that take input — the
        # next keystroke lands in the argument position without the
        # user having to space first.
        payload = f"{label} " if hint else label
        self._compose.stage_text(payload)
        self._compose.focus()

    # ── CWD palette ──────────────────────────────────────────────
    #
    # Directory-browser palette. Each `open()` call renders one
    # directory's immediate children plus a `..` parent row and an
    # `accept this dir` sentinel; picking a child re-opens the palette
    # on that dir. The commit row re-bootstraps the ACP session on the
    # new cwd (no live `set_session_cwd` in ACP today) while keeping
    # the pilot window and socket alive.

    def _open_cwd_palette(self, start_dir: Optional[str] = None) -> None:
        """Directory browser reachable via the root palette's `cwd`
        row. `start_dir` defaults to the adapter's current cwd so
        re-opens land where the user left off; navigating `..` /
        picking a child recursively calls back in with the new path."""
        if self._cwd_palette is None:
            self._cwd_palette = CommandPalette(
                host_overlay=self._compose_overlay,
                on_commit=self._commit_cwd_choice,
                on_cancel=self._compose.focus,
                select_mode=True,
                placeholder=(
                    "Pick cwd — Enter descends · Esc cancels · "
                    "select the dir row to commit"
                ),
            )
        current = start_dir or getattr(self._adapter, "cwd", None) or os.getcwd()
        try:
            current = os.path.abspath(current)
        except Exception:
            current = os.getcwd()
        self._size_palette(self._cwd_palette)
        self._cwd_palette.preseed_active(set())
        self._cwd_palette.open(self._collect_cwd_entries(current))

    def _collect_cwd_entries(self, directory: str) -> list[tuple[str, str, str, str]]:
        """Build palette rows for `directory`:

        * `cwd-accept` — sentinel committing the browsed dir as the
          new adapter cwd. Appears first so Enter on an unfiltered
          list commits without extra navigation.
        * `cwd-parent` — `..` row. Renders as the parent path so the
          user sees where they'd land.
        * `cwd-dir` — one per immediate subdirectory, including
          dotfiles (the user may want `.config` / `.cache`). Sorted
          case-insensitively.
        """
        out: list[tuple[str, str, str, str]] = [
            (
                "cwd-accept",
                f" use {directory}",
                "commit this directory as the agent's cwd",
                directory,
            ),
        ]
        parent = os.path.dirname(directory.rstrip("/"))
        if parent and parent != directory:
            out.append(
                (
                    "cwd-parent",
                    "..",
                    f"go up to {parent}",
                    parent,
                )
            )
        try:
            entries = sorted(os.listdir(directory), key=str.lower)
        except OSError as e:
            log.warning("cwd palette: listdir(%s) failed: %s", directory, e)
            entries = []
        for name in entries:
            full = os.path.join(directory, name)
            try:
                is_dir = os.path.isdir(full)
            except OSError:
                is_dir = False
            if not is_dir:
                continue
            out.append(("cwd-dir", name, full, full))
        return out

    def _commit_cwd_choice(self, entries) -> None:
        """Select-mode commit: dispatches on the row's kind.

        * `cwd-parent` / `cwd-dir` re-open the palette at the new
          path — the user is still browsing.
        * `cwd-accept` calls `_apply_cwd` which re-bootstraps the
          adapter session at the new cwd while keeping the pilot
          window + socket alive (equivalent to `start_fresh_session`
          with a new cwd baked in)."""
        if not entries:
            self._compose.focus()
            return
        kind, _name, _desc, preview = entries[0]
        if kind in ("cwd-parent", "cwd-dir") and preview:
            self._open_cwd_palette(start_dir=preview)
            return
        if kind == "cwd-accept" and preview:
            self._apply_cwd(preview)
        self._compose.focus()

    def _apply_cwd(self, new_cwd: str) -> None:
        """Point the adapter at `new_cwd` and re-bootstrap the session.

        ACP has no live `set_session_cwd`, so we mutate the adapter's
        `cwd`, drop the current ACP session, and let `start_fresh_session`
        wipe the transcript + re-arm the AGENTS.md prefix. The pilot
        window + Unix socket stay alive — the keybinding stays
        wired to the same pilot instance.

        `self._cwd` tracks the same value so the breadcrumb pill +
        tooltip read off one source; `_refresh_session_label` inside
        `start_fresh_session` then paints the new path immediately."""
        if not new_cwd:
            return
        try:
            resolved = os.path.abspath(new_cwd)
        except Exception:
            resolved = new_cwd
        log.info("cwd: switching adapter cwd to %s", resolved)
        self._adapter.cwd = resolved
        self._cwd = resolved
        self.start_fresh_session()

    def _restore_session(self, session_id: str) -> None:
        """Swap the adapter onto `session_id`, wipe the visible
        transcript, then repaint the replayed history the agent pushed
        during `session/load`. Mirrors `start_fresh_session` for in-
        memory cleanup — the only differences are the adapter call
        (`select_session` + `start` vs `reset`) and the replay pass at
        the end."""
        if not session_id:
            return
        log.info("_restore_session: switching to id=%s", session_id)
        self._set_working(f"LOADING {session_id[:8]}")
        try:
            self._adapter.select_session(session_id)
            # Force the bootstrap now so the agent's session/load replay
            # (which streams `session/update` notifications BEFORE the
            # load response returns) is buffered and drainable via
            # `replay_chunks` immediately — without this the user has
            # to send a follow-up turn before history appears.
            start = getattr(self._adapter, "start", None)
            if callable(start):
                start()
        except Exception as e:
            log.warning("adapter select_session raised: %s", e)
            self._clear_working()
            return
        self._clear_working()

        # Same housekeeping as `start_fresh_session` — the old
        # transcript, queue, and pending UI state belonged to the
        # session we just unhooked.
        self._streaming = False
        self._stream_started = False
        self._turn_cancelled = False
        self._active_assistant = None
        for card in list(self._cards):
            try:
                self._conv_box.remove(card.widget)
            except Exception:
                pass
        self._cards.clear()
        for row in list(self._queue):
            try:
                self._queue_listbox.remove(row)
            except Exception:
                pass
        self._queue.clear()
        self._queue_box.set_visible(False)
        for row in list(self._permissions):
            try:
                self._permissions_listbox.remove(row)
            except Exception:
                pass
        self._permissions.clear()
        self._permissions_box.set_visible(False)
        self._last_plan_card = None
        self._last_plan_items = []
        if self._pending_resources:
            self._pending_resources = []
            self._refresh_resource_pills()
        if self._pending_attachments:
            self._pending_attachments = []
            self._refresh_attachment_pills()
        self._session_title = ""
        self._update_phase()
        self._refresh_session_label()
        self._refresh_session_title_label()
        if hasattr(self, "_provider_label"):
            self._provider_label.set_label(self._header_title())
        _signal_waybar_safe()
        self._replay_session_history()

    def _replay_session_history(self) -> None:
        """Drain `replay_chunks` and paint each event onto a fresh
        card stack.

        UserMessageChunk rolls forward into a new user card and opens
        a fresh assistant card to collect the next segment of agent
        output. Agent text / thinking / tool / plan / session_info
        chunks land on the currently-open assistant card.

        No-op when the adapter can't replay (fresh session, or non-ACP
        adapter)."""
        replay = getattr(self._adapter, "replay_chunks", None)
        if not callable(replay):
            return
        chunks = list(replay())
        if not chunks:
            return
        log.info("replay: painting %d chunks", len(chunks))
        assistant: Optional[TurnCard] = None
        for chunk in chunks:
            if isinstance(chunk, UserMessageChunk):
                self._append_user_card(chunk.text)
                assistant = self._append_assistant_card()
                continue
            if assistant is None:
                # Agent-initiated replay with no preceding user turn
                # (rare; covers agents that summarise their own state
                # before the first user prompt).
                assistant = self._append_assistant_card()
            if isinstance(chunk, ToolCall):
                assistant.append_tool_bubble(chunk)
            elif isinstance(chunk, ThinkingChunk):
                assistant.append_thinking(chunk.text)
            elif isinstance(chunk, PlanChunk):
                assistant.set_plan(chunk.items)
                self._last_plan_card = assistant
                self._last_plan_items = list(chunk.items)
            elif isinstance(chunk, SessionInfoChunk):
                self._apply_session_info(chunk.title)
            else:
                # Plain text chunk.
                assistant.append(str(chunk))
        self._force_scroll_to_bottom()

    def start_fresh_session(self) -> None:
        """Hard reset: drop the ACP session + wipe the visible
        conversation. Used by the sessions palette's "new session"
        sentinel.

        Actions:
          1. `_adapter.reset()` — tears down subprocess, unlinks the
             store file, re-arms AGENTS.md for the first turn of the
             replacement session.
          2. Clear the transcript stack (cards, active-assistant
             pointer, plan cache, tool-bubble state).
          3. Clear the queue (pending outgoing turns would land on the
             NEW session which hasn't been told about them; safer to
             drop them than silently retarget).
          4. Drop pending permission rows — they referenced tool calls
             from the OLD session's in-flight turn, which cancel() /
             teardown already invalidated.
          5. Clear any pending compose attachments / resource pills
             that were queued for submission.
          6. Reset phase so the provider pill returns to `idle`.

        After this, the next `dispatch_turn` / staged submission
        triggers `_ensure_started` against a fresh session_id."""
        log.info("start_fresh_session: tearing down + wiping transcript")
        try:
            self._adapter.reset()
        except Exception as e:
            log.warning("adapter reset raised: %s", e)

        # Stream/turn flags.
        self._streaming = False
        self._stream_started = False
        self._turn_cancelled = False
        self._active_assistant = None

        # Wipe every card out of the conversation scroller.
        for card in list(self._cards):
            try:
                self._conv_box.remove(card.widget)
            except Exception:
                pass
        self._cards.clear()

        # Queue goes too — queued rows target a session that no
        # longer exists.
        for row in list(self._queue):
            try:
                self._queue_listbox.remove(row)
            except Exception:
                pass
        self._queue.clear()
        self._queue_box.set_visible(False)

        # Pending permission rows from the old in-flight turn.
        for row in list(self._permissions):
            try:
                self._permissions_listbox.remove(row)
            except Exception:
                pass
        self._permissions.clear()
        self._permissions_box.set_visible(False)

        # Plan cache — Ctrl+O should not reopen the previous session's
        # plan after the user explicitly asked for a fresh start.
        self._last_plan_card = None
        self._last_plan_items = []

        # Clear pending compose state so stale resources / attachments
        # don't ride on the first turn of the new session.
        if self._pending_resources:
            self._pending_resources = []
            self._refresh_resource_pills()
        if self._pending_attachments:
            self._pending_attachments = []
            self._refresh_attachment_pills()

        self._update_phase()
        # `_adapter.reset()` zeroed `session_resumed`; refresh the
        # breadcrumb so the `󰑐 restored` tag drops in the same frame
        # as the transcript wipe.
        self._refresh_session_label()
        _signal_waybar_safe()

    def _collect_resources(self) -> list[tuple[str, str, str, str]]:
        """Build the `(kind, name, description, preview)` list feeding
        the skills palette. Sourced via our own MCP server's
        `resources/list` so the palette and the agent see the same
        set.

        `name` slot carries `skill.title or skill.name` — the MCP
        listing now emits the human-readable title alongside the slug
        (spec 2025-03) and the user wants that at the top of each row.
        `_resolve_resource` falls back to a title→slug scan so the
        pill the user picks (which carries the title as its key)
        still resolves to the right SKILL.md folder."""
        resources: list[tuple[str, str, str, str]] = []
        if self._skills_dir:
            for skill in list_skills_via_mcp(
                _PILOT_MCP_SCRIPT, skills_dir=self._skills_dir
            ):
                display = skill.title or skill.name
                resources.append(
                    ("skill", display, skill.description, skill.uri)
                )
        return resources

    def _collect_references(self) -> list[tuple[str, str, str, str]]:
        """Build the palette entries for the references palette — same
        MCP source as skills, filtered for `reference/*` URIs. Filename
        slug is the display label (references don't ship titles)."""
        out: list[tuple[str, str, str, str]] = []
        if self._skills_dir:
            for ref in list_references_via_mcp(
                _PILOT_MCP_SCRIPT, skills_dir=self._skills_dir
            ):
                display = ref.title or ref.name
                out.append(
                    ("reference", display, ref.description, ref.uri)
                )
        return out

    def _collect_permission_entries(self) -> list[tuple[str, str, str, str]]:
        """Build palette entries for the Ctrl+K permissions view. Each
        row's `kind` matches the permission bucket (`trusted` /
        `auto_approve` / `auto_reject`) so the commit handler can drop
        the tool from the right set."""
        out: list[tuple[str, str, str, str]] = []
        for name in sorted(self._permission.trusted):
            out.append(("trusted", name, "trusted · click to revoke", name))
        for name in sorted(self._permission.auto_approved):
            out.append(("auto_approve", name, "auto-approve · click to drop", name))
        for name in sorted(self._permission.auto_rejected):
            out.append(("auto_reject", name, "auto-reject · click to drop", name))
        return out

    def _accept_first_permission(self) -> None:
        """Ctrl+G: click the `󰄬 allow` button on the oldest pending
        permission row. Keyboard-only accept for the row that grabbed
        focus when it appeared — saves the user a Tab-to-allow + Enter
        dance when they just want to approve and move on. Silent no-op
        when the panel is empty."""
        if not self._permissions:
            return
        self._permissions[0]._allow_btn.emit("clicked")

    def _reject_first_permission(self) -> None:
        """Ctrl+R: click the `󰅖 deny` button on the oldest pending
        permission row — symmetric to Ctrl+G. Cancels the current
        turn and drops the tool from trust if it was there. Silent
        no-op when the panel is empty."""
        if not self._permissions:
            return
        self._permissions[0]._deny_btn.emit("clicked")

    def _toggle_last_thinking(self) -> None:
        """Ctrl+T: flip the open state on the most recent assistant
        card's thinking expander. Walks `self._cards` backwards so
        the latest reasoning block wins; silent no-op when nothing in
        the conversation produced one."""
        for card in reversed(self._cards):
            if card.toggle_thinking():
                return

    def _open_last_plan(self) -> None:
        """Ctrl+O: re-expand the most recent plan block and scroll it
        into view. If the plan card auto-collapsed after all items
        finished, this reopens it so the user can re-read the
        finalised list. Silent no-op when no plan has landed yet."""
        card = self._last_plan_card
        if card is None or not card.has_plan():
            # Fall back to walking cards — handles the edge case of a
            # turn replay that landed plans on an older card.
            for candidate in reversed(self._cards):
                if candidate.has_plan():
                    card = candidate
                    break
        if card is None or not card.has_plan():
            return
        card.toggle_plan()
        # Ensure it ends up expanded regardless of prior state — Ctrl+O
        # is "show me the plan", not a toggle.
        if card._plan_expander is not None and not card._plan_expander.get_expanded():
            card._plan_expander.set_expanded(True)
        try:
            card.widget.grab_focus()
        except Exception:
            pass

    def _yank_last_assistant(self) -> None:
        """Ctrl+Y: copy the most recent assistant reply to the Wayland
        clipboard via `wl-copy`. Walks `self._cards` backwards so the
        most-recent completed assistant turn wins. Silent no-op when
        no assistant has replied yet."""
        for card in reversed(self._cards):
            if card.role != "assistant":
                continue
            text = card.get_text()
            if not text:
                continue
            try:
                OutputAdapterClipboard().write(text)
            except Exception as e:
                log.warning("yank-to-clipboard failed: %s", e)

            return

    def _on_close_request(self, _window) -> bool:
        self._alive = False
        try:
            self._adapter.close()
        except Exception as e:
            log.warning("adapter close failed: %s", e)
        _signal_waybar_safe()

        return False

    # -- Statics --------------------------------------------------------

    def _on_monitor_bound(self, monitor) -> None:
        """`LayerOverlayWindow` hook: called after every
        `_bind_to_focused_monitor` resize. We cap the compose scroller
        at 25% of the bound monitor's height so a tall conversation
        can't push the compose box off-screen, and re-apply each
        pending permission row's body fraction so a monitor hop
        re-sizes long arg renders to the new overlay height."""
        if monitor is None:
            return
        height = monitor.get_geometry().height
        self._compose.set_max_content_fraction(height, 0.25)
        for row in self._permissions:
            row.apply_height_fraction(height)

    @staticmethod
    def _install_css() -> None:
        """Install the shared overlay CSS first, then layer pilot.css on
        top at the same priority (USER+1).

        A USER-priority `~/.config/gtk-4.0/gtk.css` beats
        APPLICATION-priority rules regardless of selector specificity —
        Graphite-style themes installed there ship `textview text {
        background-color: #0F0F0F }` which paints opaque black behind
        every TextView's text subnode in the overlay. `USER + 1` is the
        smallest bump that beats `~/.config/gtk-4.0/gtk.css` without
        stomping on anything the user might layer on top intentionally.
        Loading the shared overlay CSS FIRST means pilot-specific rules
        (with the same priority but added later) win on ties.

        Parsing errors are routed to our logger so missing selectors /
        bad rule bodies surface in `-v` runs instead of vanishing."""
        load_overlay_css()
        pilot_css_path = os.path.join(os.path.dirname(__file__), "pilot.css")
        load_css_from_path(pilot_css_path, tag="pilot.css")

class Session:
    """Owns the Unix socket for a live pilot window. A background thread
    accepts connections from forwarder invocations and dispatches their
    `turn` / `status` commands back onto the GTK main thread."""

    @staticmethod
    def is_live() -> bool:
        """Probe the session socket. True if a server accepted our
        connect, False when the file is stale / absent."""
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(1)
        try:
            probe.connect(_PATHS.socket_path)
            return True
        except ConnectionRefusedError, FileNotFoundError:
            return False
        except OSError as e:
            log.warning("socket probe failed: %s", e)
            return False
        finally:
            probe.close()

    @staticmethod
    def send(cmd: str, **kwargs) -> Optional[dict]:
        """Send a one-shot JSON command to the running session.

        Returns the parsed response dict, or None when no session
        answers. Stale socket files from a crashed session get
        unlinked so the next invocation can bind fresh."""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2)
        try:
            sock.connect(_PATHS.socket_path)
        except FileNotFoundError, ConnectionRefusedError:
            try:
                os.unlink(_PATHS.socket_path)
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

    def __init__(self, window: PilotWindow, provider: ConversationProvider):
        self._window = window
        self._provider = provider
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None

    def _bind(self, path: str) -> socket.socket:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(path)
        except OSError as e:
            if e.errno != errno.EADDRINUSE:
                sock.close()
                raise
            if Session.is_live():
                sock.close()
                raise RuntimeError(
                    f"another pilot session is already running at {path}"
                )
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            sock.bind(path)
        os.chmod(path, 0o600)
        sock.listen(4)
        return sock

    def start(self) -> None:
        self._sock = self._bind(_PATHS.socket_path)
        self._thread = threading.Thread(
            target=self._serve, args=(self._sock,), daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        try:
            os.unlink(_PATHS.socket_path)
        except FileNotFoundError:
            pass

    def _serve(self, listener: socket.socket) -> None:
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            background_tasks.submit(
                "pilot-session-handler", lambda c=conn: self._handle(c)
            )

    def _handle(self, conn: socket.socket) -> None:
        try:
            raw = conn.recv(8192).decode("utf-8", errors="replace").strip()
            response = self._dispatch(raw)
            try:
                conn.sendall(json.dumps(response).encode())
            except BrokenPipeError, ConnectionResetError:  # noqa
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
                    # Always STAGE — never auto-dispatch external input.
                    # The user presses Enter to send. `stage_turn` also
                    # presents the overlay if it was hidden, so speech
                    # press-2 forwards still surface the text to the
                    # user; they just get to review before the turn
                    # goes out. Match for fresh-spawn stdin too (see
                    # `_cmd_toggle`'s `on_activate`).
                    GLib.idle_add(self._window.stage_turn, text)

                return {"ok": True}
            case "status":
                adapter = self._window.adapter()
                return {
                    "ok": True,
                    "phase": self._window.phase(),
                    "provider": self._provider.value,
                    "model": getattr(adapter, "model", "") or "",
                    "queue": self._window.queue_size(),
                    "session": _PATHS.suffix,
                    "session_id": getattr(adapter, "session_id", None) or "",
                    "session_resumed": bool(getattr(adapter, "session_resumed", False)),
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

_PILOT_MCP_SERVER_NAME = "system"
_PILOT_MCP_SCRIPT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "lib", "mcp_server.py"
)

def _build_pilot_mcp_server(skills_dir: Optional[str]) -> McpServerStdio:
    """Construct the `system` ACP server pilot ships itself. Kept in
    pilot.py (not `lib.mcp_servers`) so the env — currently just
    `PILOT_SKILLS_DIR` — is resolved from pilot's own argparse state
    with no indirection through placeholder substitution."""
    from acp.schema import EnvVariable, McpServerStdio

    env: list[EnvVariable] = []
    if skills_dir:
        env.append(EnvVariable(name="PILOT_SKILLS_DIR", value=skills_dir))
    return McpServerStdio(
        name=_PILOT_MCP_SERVER_NAME,
        command=sys.executable,
        args=["-u", _PILOT_MCP_SCRIPT],
        env=env,
    )

def _acp_mcp_servers(
    mcp: Optional[list[str]] = None,
    *,
    skills_dir: Optional[str] = None,
) -> list:
    """Build the `new_session.mcp_servers` payload for the ACP adapters.
    Two sources are merged: pilot's built-in `system` server (config
    assembled inline) and every mcphub-catalog name the user opted
    into via `--mcp`. Unknown catalog names log and skip."""
    from lib.converse import build_mcp_servers

    names = [(raw or "").strip() for raw in mcp or []]
    names = [n for n in names if n]

    out: list = []
    if _PILOT_MCP_SERVER_NAME in names:
        out.append(_build_pilot_mcp_server(skills_dir))

    external_specs: dict[str, dict] = {}
    for name in names:
        if name == _PILOT_MCP_SERVER_NAME or name in external_specs:
            continue
        try:
            external_specs[name] = _DEFAULT_SERVER_GET(name)
        except KeyError as e:
            log.warning("ignoring unknown --mcp %r: %s", name, e)
    out.extend(build_mcp_servers(external_specs))
    log.info("ACP mcp_servers attached: %s", [s.name for s in out])
    return out

def _build_permission_handler(window):
    """Adapt the overlay's `show_permission_for_acp` main-thread entry
    point into the blocking `PermissionHandler` signature the ACP
    worker thread expects. Returns a callable that:

    1. Schedules the PermissionRow on the GTK thread via `idle_add`,
       guarded by try/except so a raise inside the GTK handler still
       unblocks the ACP worker instead of wedging it for the full
       timeout window.
    2. Blocks on a `threading.Event` until the user clicks OR the
       timeout fires.
    3. Returns the chosen `option_id` (or `None` to send `cancelled`).

    On timeout we log and return None — the ACP request_permission
    caller converts None → `DeniedOutcome(cancelled)` so the agent
    resumes instead of hanging."""

    # Shorter than the legacy 10 minutes: opencode issue #12133 (the
    # hang the user reported) surfaces when no response comes back
    # within seconds, and the UX of "click deny or wait 10 minutes" is
    # strictly worse than "click deny within 2 minutes or we auto-deny
    # and you can retry". The upper bound still covers every realistic
    # think-time for a permission decision.
    timeout_s = 120.0

    def handler(call, options):
        import time as _time

        from lib.converse import ToolCall as _ToolCall

        # ACP gives us a ToolCallSummary; adapt to the ToolCall the
        # PermissionRow already renders. Status=running signals
        # "awaiting approval, tool hasn't executed yet". `title` and
        # `kind` travel through so the row can show the agent's
        # human-readable header (Claude: "Read README.md", opencode:
        # "edit") on top of the canonical `name` used for trust.
        tool_call = _ToolCall(
            tool_id=call.tool_id,
            name=call.name,
            arguments=call.arguments,
            status="running",
            audit=False,
            title=call.title,
            kind=call.kind,
        )
        event = threading.Event()
        result: dict[str, Optional[str]] = {"option_id": None}
        start = _time.monotonic()

        def resolve(option_id: Optional[str]) -> None:
            result["option_id"] = option_id
            event.set()

        def idle_wrap(call_arg, options_arg, resolve_arg) -> bool:
            # GTK's `idle_add` swallows exceptions raised inside the
            # callback; without this wrapper a bug in
            # `show_permission_for_acp` would leave `event` unset and
            # the worker thread blocked on `event.wait(timeout)`. The
            # ACP agent sees nothing and hangs. Wrap + resolve(None)
            # converts the bug into a clean "deny" response so the
            # agent unblocks immediately.
            try:
                window.show_permission_for_acp(call_arg, options_arg, resolve_arg)
            except Exception as e:
                log.exception("show_permission_for_acp raised: %s", e)
                resolve_arg(None)
            return False  # GDK_SOURCE_REMOVE

        GLib.idle_add(idle_wrap, tool_call, options, resolve)
        if not event.wait(timeout=timeout_s):
            log.warning(
                "acp permission prompt timed out after %.0fs (tool=%s); "
                "sending cancelled so the agent can resume",
                timeout_s,
                call.name,
            )
            return None
        dt_ms = int((_time.monotonic() - start) * 1000)
        chosen = result["option_id"]
        log.info(
            "acp permission responded: tool=%s option_id=%s dt_ms=%d",
            call.name,
            chosen,
            dt_ms,
        )
        return chosen

    return handler

_MCP_SPLIT_RE = re.compile(r"[\s,]+")

def _resolve_mcp(args) -> list[str]:
    """De-dupe `--mcp NAME` flags into an ordered list. Each flag
    value may itself be comma- or whitespace-separated. Omitting the
    flag picks the full catalog; `--mcp ""` disables everything."""
    raw_values = getattr(args, "mcp", None)
    if raw_values is None:
        return [_PILOT_MCP_SERVER_NAME, *DEFAULT_SERVER_NAMES]
    out: list[str] = []
    seen: set[str] = set()
    for raw in raw_values:
        for piece in _MCP_SPLIT_RE.split(raw or ""):
            name = piece.strip()
            if name and name not in seen:
                seen.add(name)
                out.append(name)
    return out

def _read_agents_md(path: Optional[str]) -> str:
    """AGENTS.md contents (or empty string on missing / unreadable)."""
    if not path:
        log.info("agents-md: no path configured; injection disabled")
        return ""
    expanded = os.path.expanduser(path)
    if not os.path.isfile(expanded):
        log.warning("agents-md %s: file not found; skipping injection", expanded)
        return ""
    try:
        with open(expanded, "r", encoding="utf-8") as f:
            contents = f.read().strip()
    except OSError as e:
        log.warning("agents-md %s: read failed (%s); skipping injection", expanded, e)
        return ""
    log.info("agents-md %s: loaded %d chars", expanded, len(contents))
    return contents

def _build_adapter(args) -> ConversationAdapter:
    provider = ConversationProvider(args.converse_provider)
    cwd = getattr(args, "cwd", None)
    skills_dir = os.path.expanduser(getattr(args, "skills_dir", "") or "") or None
    mcp_servers = _acp_mcp_servers(
        mcp=_resolve_mcp(args),
        skills_dir=skills_dir,
    )
    agents_md = _read_agents_md(getattr(args, "agents_md", None))
    system_prompt = (
        f"{agents_md}\n\n{AI_SYSTEM_PROMPT}" if agents_md else AI_SYSTEM_PROMPT
    )
    log.info(
        "_build_adapter: provider=%s model=%s cwd=%s",
        provider.value,
        args.converse_model,
        cwd,
    )
    match provider:
        case ConversationProvider.CLAUDE:
            return ConversationAdapterClaude(
                system_prompt,
                model=args.converse_model,
                cwd=cwd,
                mcp_servers=mcp_servers,
            )
        case ConversationProvider.OPENCODE:
            return ConversationAdapterOpenCode(
                system_prompt,
                model=args.converse_model,
                cwd=cwd,
                mcp_servers=mcp_servers,
            )
        case _:
            raise ValueError(f"unknown converse provider: {provider!r}")

def _read_input(mode: InputMode) -> str:
    match mode:
        case InputMode.STDIN:
            # Only block on stdin when it's actually a pipe. Running
            # `pilot.py toggle` from a TTY (or via a compositor bind
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

def _toggle(args) -> None:
    """Unified toggle. Three behaviours, chosen from context:

    1. No session + any input     -> open a new session.
    2. Session + non-empty input  -> forward the input as a turn.
    3. Session + empty input      -> flip overlay visibility,
       *unless* the empty input came from a closed pipe (press-2 of
       a speech toggle pair dumping nothing into us) — in that case
       leave the session alone.
    """
    initial = _read_input(args.input)
    # stdin being a TTY means the user invoked pilot.py from a terminal
    # or a compositor bind with nothing piped in; a closed/empty pipe
    # means the upstream process exited without writing (common with
    # press-2 of `speech.py toggle --output stdout | pilot.py toggle`).
    piped_empty = args.input == InputMode.STDIN and not sys.stdin.isatty()

    status = Session.send("status")
    if status and status.get("ok"):
        if initial:
            Session.send("turn", text=initial)
            return
        if piped_empty:
            # Fire-and-forget callers (speech press-2) — don't touch
            # the visibility; the payload-bearing sibling pipe will
            # reach the session on its own.
            return
        Session.send("toggle-window")

        return

    # Fresh session path. Fall back to an auto-created tempdir when
    # `--cwd` wasn't provided — done here (not in main) so the path is
    # only created on the branch that actually spawns an adapter.
    if getattr(args, "cwd", None) is None:
        args.cwd = tempfile.mkdtemp(prefix="pilot-")

    adapter = _build_adapter(args)

    app = Gtk.Application(application_id=_PATHS.app_id)
    session: dict[str, Optional[Session]] = {"server": None}

    # MCP server names for the palette's `#{mcp/<name>}` references.
    mcp_server_names = adapter.mcp_server_names
    # Mirror mcphub's per-server `autoApprove` / `disabled_tools` lists
    # into pilot's permission state so pre-sanctioned read-only tools
    # auto-approve and known-dangerous ones auto-reject without popping
    # a row. CLI `--auto-approve` / `--auto-reject` append on top.
    seeded_approve, seeded_reject = get_permission_seeds(mcp_server_names)
    auto_approve = seeded_approve + list(getattr(args, "auto_approve", None) or [])
    auto_reject = seeded_reject + list(getattr(args, "auto_reject", None) or [])
    # Expand `~` ONCE before handing it to the window — the MCP
    # subprocess runs with its own `os.environ` and doesn't re-expand
    # shell metachars, so passing a literal `~/…` yields zero skills.
    skills_dir = os.path.expanduser(getattr(args, "skills_dir", "") or "") or None

    def on_activate(application):
        window = PilotWindow(
            application,
            adapter,
            session_suffix=_PATHS.suffix,
            auto_approve=auto_approve,
            auto_reject=auto_reject,
            cwd=getattr(args, "cwd", None),
            skills_dir=skills_dir,
            mcp_server_names=mcp_server_names,
        )
        server = Session(window, adapter.provider)
        window.attach_session(server)
        adapter.set_permission_handler(_build_permission_handler(window))
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
            # Stage rather than dispatch: spawn-time input lands in the
            # compose box for user confirmation. Same rule as the live
            # socket-turn path — external input NEVER auto-submits, it
            # always waits for Enter so transcription errors / stray
            # pastes can be corrected before the turn goes out.
            window.stage_turn(initial)

    app.connect("activate", on_activate)
    try:
        app.run([sys.argv[0]])
    finally:
        server = session.get("server")
        if server:
            server.stop()
        _signal_waybar_safe()

class Pilot:
    """CLI dispatcher — all subcommands live here as methods."""

    @dataclass
    class ToggleArgs:
        input: InputMode
        converse_provider: str
        converse_model: Optional[str]
        cwd: Optional[str]
        auto_approve: list[str]
        auto_reject: list[str]
        agents_md: str
        skills_dir: str
        mcp: Optional[list[str]]

    @click.group(context_settings={"help_option_names": ["-h", "--help"]})
    @click.option("-v", "--verbose", is_flag=True, help="Enable debug logging.")
    @click.option(
        "--session",
        "session_suffix",
        default="",
        metavar="SUFFIX",
        help="Session suffix.",
    )
    def cli(verbose: bool, session_suffix: str):
        """Conversational AI sidebar overlay."""
        create_logger(verbose)
        global _PATHS
        _PATHS = PilotPaths.from_suffix(session_suffix or "")

    @cli.command("toggle")
    @click.option(
        "--input",
        "input_",
        type=click.Choice([m.value for m in (InputMode.STDIN, InputMode.CLIPBOARD)]),
        default=InputMode.STDIN.value,
        help="Initial user-turn source.",
    )
    @click.option(
        "--converse-provider",
        type=click.Choice([p.value for p in ConversationProvider]),
        default=DEFAULT_CONVERSE_ADAPTER.value,
    )
    @click.option("--converse-model", default=None, help="Per-adapter model override.")
    @click.option(
        "--cwd",
        default=None,
        metavar="PATH",
        help="Agent working dir; defaults to a fresh tempdir.",
    )
    @click.option(
        "--auto-approve",
        "auto_approve",
        multiple=True,
        metavar="TOOL",
        help="Auto-approve a tool; repeatable.",
    )
    @click.option(
        "--auto-reject",
        "auto_reject",
        multiple=True,
        metavar="TOOL",
        help="Auto-reject a tool; repeatable.",
    )
    @click.option(
        "--agents-md",
        default="~/.config/nvim/utils/agents/AGENTS.md",
        metavar="PATH",
        help="Path to AGENTS.md; empty disables injection.",
    )
    @click.option(
        "--skills-dir",
        default="~/.config/nvim/utils/agents/skills",
        metavar="DIR",
        help="Agent skills root; empty disables.",
    )
    @click.option(
        "--mcp",
        multiple=True,
        metavar="NAME",
        help="MCP server; repeatable, accepts comma/space lists.",
    )
    def cmd_toggle(
        input_,
        converse_provider,
        converse_model,
        cwd,
        auto_approve,
        auto_reject,
        agents_md,
        skills_dir,
        mcp,
    ):
        """Open the overlay, or forward a turn to the live session."""
        _toggle(
            Pilot.ToggleArgs(
                input=InputMode(input_),
                converse_provider=converse_provider,
                converse_model=converse_model,
                cwd=os.path.expanduser(cwd) if cwd else None,
                auto_approve=list(auto_approve),
                auto_reject=list(auto_reject),
                agents_md=agents_md,
                skills_dir=skills_dir,
                mcp=list(mcp) if mcp else None,
            )
        )

    @cli.command("status")
    def cmd_status():
        """Print waybar-shaped status JSON."""
        resp = Session.send("status")
        if not resp or not resp.get("ok"):
            print(json.dumps({"class": "idle", "text": "", "tooltip": "Pilot idle"}))
            return
        provider = resp.get("provider", "")
        phase = resp.get("phase", "idle")
        queue = int(resp.get("queue", 0) or 0)
        session = resp.get("session") or _PATHS.suffix
        session_tag = f" ({session})" if session else ""
        badge = f"<sup>{queue}</sup>" if queue > 0 else ""
        text = f"󱍊{session_tag}{badge}"
        label = f"Pilot{' ' + session_tag if session_tag else ''}"
        tooltips = {
            "streaming": f"{label}: streaming via {provider}",
            "pending": f"{label}: waiting on first chunk from {provider}",
            "awaiting": f"{label}: waiting on user input ({provider})",
        }
        tooltip = tooltips.get(phase, f"{label}: {provider} idle")
        if queue > 0:
            tooltip += f"  ({queue} queued)"
        print(json.dumps({"class": phase, "text": text, "tooltip": tooltip}))

    @cli.command("is-running")
    def cmd_is_running():
        """Exit 0 if a session is live."""
        sys.exit(0 if Session.is_live() else 1)

    @cli.command("kill")
    def cmd_kill():
        """Terminate the running session."""
        if not Session.send("kill"):
            try:
                os.unlink(_PATHS.socket_path)
            except FileNotFoundError:
                pass
        _signal_waybar_safe()

# Early shebang scan expects the literal "toggle" token somewhere in argv
# — we only LD_PRELOAD gtk4-layer-shell when the window will actually
# render. Click's subcommand name matches this, so nothing else to wire.

if __name__ == "__main__":
    Pilot.cli()
