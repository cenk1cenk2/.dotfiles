#!/usr/bin/env python3
"""pilot — GTK4 layer-shell sidebar that streams a conversational AI response.

Right-side full-height overlay with a markdown scroller and a compose entry
at the bottom. Reads initial text from stdin or clipboard, sends it as the
first user turn, and streams chunks back via a `ConversationAdapter`. A
Unix-socket session lets subsequent invocations forward follow-up turns
into the live window instead of opening a new one."""

from __future__ import annotations

import argparse
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
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    # Only pulled in for the `_build_pilot_mcp_server` return annotation;
    # the real import happens inside the function body so the schema
    # module isn't forced on callers that never build an ACP server.
    from acp.schema import McpServerStdio

from lib import (
    DEFAULT_CONVERSE_ADAPTER,
    DEFAULT_SERVER_NAMES,
    ConversationAdapter,
    ConversationAdapterClaude,
    ConversationAdapterOpenCode,
    ConversationProvider,
    InputAdapterClipboard,
    InputAdapterStdin,
    InputMode,
    MarkdownMarkup,
    OutputAdapterClipboard,
    PermissionState,
    PlanChunk,
    PromptAttachment,
    ThinkingChunk,
    ToolCall,
    format_tool_args_md,
    get_permission_seeds,
    get_server as _DEFAULT_SERVER_GET,
    load_prompt,
    notify,
    signal_waybar,
)
from lib.skills import (
    list_skills_via_mcp,
    load_skill_references,
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
    load_overlay_css,
    load_css_from_path,
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

# Populated in main() once `--session` has been parsed. Module-level
# holder so the waybar-poll helpers (_send, _is_live, _cmd_status) can
# share it without every caller threading a paths argument through.
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
            label="Enter · Shift+Enter newline · Ctrl+D interrupt · Ctrl+G accept · Ctrl+R reject · Ctrl+P paste · Ctrl+Y yank · ESC hide · Ctrl+Q quit",
            xalign=0.0,
            hexpand=True,
        )
        hint.add_css_class("pilot-compose-hint")
        self._hint_label = hint
        bar.append(hint)

        self._send_btn = Gtk.Button(label="⏎ send")
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
        name)` fires when the pill's ✕ is clicked. Empty list hides
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
            btn = Gtk.Button(label=f"{kind}/{name} ✕")
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
        `(label, mime, key)` — the pill shows `label` and, on ✕,
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
            btn = Gtk.Button(label=f"{label} ✕")
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

        self._edit_btn = Gtk.Button(label="✎ edit")
        self._edit_btn.add_css_class("pilot-queue-edit")
        self._edit_btn.set_tooltip_text("Edit this message")
        self._edit_btn.connect("clicked", lambda _b: self._toggle_edit())
        actions.append(self._edit_btn)

        send_btn = Gtk.Button(label="⏎ send")
        send_btn.add_css_class("pilot-queue-send")
        send_btn.set_tooltip_text("Promote and dispatch this message now")
        send_btn.connect("clicked", lambda _b: self._on_send(self))
        actions.append(send_btn)

        remove_btn = Gtk.Button(label="✕ drop")
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
            self._display = new_text
        self._label.set_label(self._display)
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

    THINKING_LABEL_STREAMING = "🧠 thinking…"
    THINKING_LABEL_DONE = "🧠 thinking"
    PLAN_LABEL_STREAMING = "📋 plan"
    PLAN_LABEL_DONE = "📋 plan · done"

    # Per-status glyph on each tool bubble. Intentionally text-only so
    # these render at the card font size without Pango fighting an
    # inline `<tt>` or PangoAttrList.
    _TOOL_STATUS_GLYPHS = {
        "pending": "⋯",
        "running": "⋯",
        "completed": "✓",
        "failed": "⚠",
        "cancelled": "✕",
    }

    def __init__(self, role: str, title: str, on_link):
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

        self._label = Gtk.Label(
            xalign=0.0,
            yalign=0.0,
            hexpand=True,
            wrap=True,
            wrap_mode=Pango.WrapMode.WORD_CHAR,
            use_markup=True,
            selectable=True,
            natural_wrap_mode=Gtk.NaturalWrapMode.WORD,
        )
        self._label.add_css_class("pilot-card-text")
        # Fire the window's link handler when the user clicks a rendered
        # `<a href="…">`. Return True to tell GTK "we handled it" — we
        # don't want the default xdg-open path because the handler may
        # want to route through a compositor-specific opener.
        self._label.connect(
            "activate-link",
            lambda _lbl, uri: (on_link(uri), True)[1],
        )
        self.widget.append(self._label)

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
        self._label.set_markup(self._md.render(self._text))

    def set_text(self, text: str) -> None:
        self._text = text
        self._label.set_markup(self._md.render(text))

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
            "completed": "✓",
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
        detail label. Called lazily when the panel becomes visible
        (initial toggle) and on every subsequent status/result
        update so the panel stays in sync.

        Args render through `format_tool_args_md` + the shared
        markdown-to-Pango pipeline so Bash commands, file diffs,
        and JSON payloads land in proper fenced code blocks instead
        of a single escaped `<tt>` line."""
        label: Gtk.Label = slot["details_label"]
        name = slot.get("name") or ""
        args = slot.get("arguments") or ""
        md_body = format_tool_args_md(name, args)
        header = f"<b>{GLib.markup_escape_text(name)}</b>"
        try:
            args_markup = self._md.render(md_body) if md_body else ""
        except Exception as e:
            log.warning("bubble markdown render failed: %s", e)
            args_markup = GLib.markup_escape_text(md_body)
        parts = [header]
        if args_markup:
            parts.append(args_markup)
        result = slot.get("result")
        if result:
            try:
                result_markup = self._md.render(str(result))
            except Exception:
                result_markup = GLib.markup_escape_text(str(result))
            parts.append(f"<i>result:</i>\n{result_markup}")
        label.set_markup("\n\n".join(parts))

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
        button.set_tooltip_text(call.name or "")
        button.set_can_focus(True)

        # Detail panel lives in the details_box (below the flow),
        # wrapped in a Revealer so toggling doesn't reshuffle layout.
        details_label = Gtk.Label(
            xalign=0.0,
            yalign=0.0,
            hexpand=True,
            wrap=True,
            wrap_mode=Pango.WrapMode.WORD_CHAR,
            use_markup=True,
            selectable=True,
            natural_wrap_mode=Gtk.NaturalWrapMode.WORD,
        )
        details_label.add_css_class("pilot-tool-bubble-details")
        revealer = Gtk.Revealer()
        revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        revealer.set_reveal_child(False)
        revealer.set_child(details_label)
        assert self._tool_details_box is not None
        self._tool_details_box.append(revealer)

        slot = {
            "tool_id": tool_id,
            "name": call.name or "",
            "arguments": call.arguments or "",
            "status": call.status or "pending",
            "result": None,
            "button": button,
            "revealer": revealer,
            "details_label": details_label,
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

_PERMISSION_MD = MarkdownMarkup()

class PermissionRow(Gtk.ListBoxRow):
    """A pending tool-use event rendered as a full-width card above the
    queue. Mirrors `QueueRow`'s structure: a wrapping body + an action
    strip with three buttons. Buttons are:

    * `✓ allow`  — dismiss the row; the call already happened server-
                   side / in-CLI, this just acknowledges it.
    * ` trust`  — add this tool name to the session allowlist so
                   future invocations skip the row entirely.
    * `✕ deny`   — cancel the in-flight turn (same path as Ctrl+D) and
                   stamp the assistant card with a cancelled marker.

    The UI is visibility-only: we don't have a protocol for gating the
    actual tool execution in any of our backends. Documented in the
    plan; user-facing copy keeps the verbs honest."""

    def __init__(
        self,
        call: ToolCall,
        on_allow,
        on_trust,
        on_deny,
        on_auto_reject=None,
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

        # Tool name — accent-coloured header so it reads as a fresh
        # event rather than another turn card. MCP tool names can be
        # very long (`mcp__mcphub__linear_kilic-dev__save_issue`); we
        # ellipsize from the LEFT so the leaf tool name (the
        # actionable bit) is always visible, and the full identifier
        # lives in the tooltip. 120-char cap keeps the row single-line
        # on the narrowest sidebar widths we ship.
        full_name = call.name or "(unnamed tool)"
        name_label = Gtk.Label(label=full_name, xalign=0.0, hexpand=True)
        name_label.add_css_class("pilot-permission-tool-name")
        name_label.set_ellipsize(Pango.EllipsizeMode.START)
        name_label.set_max_width_chars(120)
        name_label.set_tooltip_text(full_name)
        card.append(name_label)

        # Argument preview. Routes through `format_tool_args_md` so
        # common tools (Bash / Read / Edit / …) land in fenced code
        # blocks with appropriate language tags — the full command or
        # diff is visible, word-wrapped, not truncated. The label
        # width is capped via Pango's wrap-width so even a long Bash
        # command fits cleanly inside the sidebar instead of pushing
        # the row past the overlay bounds.
        md_body = format_tool_args_md(call.name or "", call.arguments or "")
        try:
            markup = _PERMISSION_MD.render(md_body)
        except Exception as e:
            log.warning("permission markdown render failed: %s", e)
            markup = GLib.markup_escape_text(md_body)
        args_label = Gtk.Label(
            xalign=0.0,
            yalign=0.0,
            hexpand=True,
            wrap=True,
            wrap_mode=Pango.WrapMode.WORD_CHAR,
            natural_wrap_mode=Gtk.NaturalWrapMode.WORD,
            use_markup=True,
            selectable=True,
        )
        args_label.set_markup(markup)
        args_label.add_css_class("pilot-permission-args")
        # Tooltip keeps the raw (non-rendered) markdown — handy for
        # copy / paste when the user wants to round-trip the exact
        # argument payload elsewhere.
        args_label.set_tooltip_text(md_body)
        card.append(args_label)

        actions = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=4,
            halign=Gtk.Align.END,
        )
        actions.add_css_class("pilot-permission-actions")

        self._allow_btn = Gtk.Button(label="✓ allow")
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

        self._deny_btn = Gtk.Button(label="✕ deny")
        self._deny_btn.add_css_class("pilot-permission-deny")
        self._deny_btn.set_tooltip_text("Cancel the current turn")
        self._deny_btn.connect("clicked", lambda _b: self._on_deny(self))
        actions.append(self._deny_btn)

        # ⛔ auto-reject — symmetric to trust but for the auto-reject
        # list: future calls for this tool short-circuit to `deny`
        # without surfacing a row, AND the current turn is cancelled
        # so the model stops mid-sentence. Red-tinted like deny to
        # signal "this is destructive in both directions".
        self._auto_reject_btn: Optional[Gtk.Button] = None
        if on_auto_reject is not None:
            self._auto_reject_btn = Gtk.Button(label="⛔ auto-reject")
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
        """Grab focus on the `✓ allow` button so keyboard users can
        accept without mousing. Tab cycles to trust / deny peers via
        GTK's default focus chain (all three buttons are focusable)."""
        self._allow_btn.grab_focus()

    @property
    def tool_name(self) -> str:
        return self._call.name or ""

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
        # Palette widget is built on demand (first Ctrl+Space) and then
        # cached — we reset state (search box, active set) on every
        # open so stale selections don't leak across sessions.
        self._palette: Optional[CommandPalette] = None
        self._permissions_palette: Optional[CommandPalette] = None
        self._mcp_palette: Optional[CommandPalette] = None
        self._sessions_palette: Optional[CommandPalette] = None
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
        # Single-line layout: provider/phase pill, dim cwd + mcp count
        # breadcrumb, close button. Fits on one row on a 400px-wide
        # sidebar; cwd uses middle-ellipsis so the most informative
        # part (project name at the tail) stays visible when the full
        # path overflows.
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        header.add_css_class("pilot-header")
        self._provider_label = Gtk.Label(label=self._header_title(), xalign=0.0)
        self._provider_label.add_css_class("pilot-provider")
        self._provider_label.add_css_class("idle")
        header.append(self._provider_label)

        self._session_label = Gtk.Label(
            label=self._session_subtitle(),
            xalign=0.0,
            hexpand=True,
        )
        self._session_label.add_css_class("pilot-session")
        self._session_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self._session_label.set_tooltip_text(self._session_subtitle(verbose=True))
        header.append(self._session_label)

        close_btn = Gtk.Button(label="✕")
        close_btn.add_css_class("pilot-close")
        close_btn.connect("clicked", lambda _b: self.close())
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
        the token in that case."""
        if not self._skills_dir:
            return None
        if kind == "skill":
            skill_md = os.path.join(self._skills_dir, name, "SKILL.md")
            skill = parse_skill(skill_md, fallback_name=name)
            if skill is None:
                return None
            refs = load_skill_references(self._skills_dir, name)
            if refs and refs.startswith("No references"):
                refs = None
            parts = [skill.body]
            if refs:
                parts.append("### References\n\n" + refs)
            return "\n\n".join(parts)
        if kind == "reference":
            return read_reference(self._skills_dir, name)
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
        )
        self._cards.append(card)
        self._conv_box.append(card.widget)
        self._force_scroll_to_bottom()
        return card

    def _header_title(self) -> str:
        if self._model:
            base = self.HEADER_WITH_MODEL_FMT.format(
                provider=self._provider_name, model=self._model
            )
        else:
            base = self.HEADER_FMT.format(provider=self._provider_name)
        if self._session_suffix:
            return f"[{self._session_suffix}] {base}"
        return base

    def _pretty_cwd(self) -> str:
        """Return a compact cwd label. Collapses `$HOME` to `~`, and
        when the full path is longer than 48 chars keeps only the last
        three segments (with a leading `…/`) so the breadcrumb stays
        one line on a ~400px-wide sidebar."""
        raw = self._cwd or os.getcwd()
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
        """Single-line breadcrumb next to the provider pill:
        `@ ~/notes  +3 mcps  +skills`. `verbose` swaps the truncated
        cwd for the full untruncated path so the tooltip can show the
        absolute path on hover."""
        cwd = self._cwd or os.getcwd() if verbose else self._pretty_cwd()
        parts = [f"@ {cwd}"]
        if self._mcp_server_names:
            parts.append(f"+{len(self._mcp_server_names)} mcps")
        if self._skills_dir:
            parts.append("+skills")
        return "  ".join(parts)

    def _refresh_session_label(self) -> None:
        """Re-render the breadcrumb — called on attach_session / every
        config change that could flip one of the three segments."""
        if hasattr(self, "_session_label"):
            self._session_label.set_label(self._session_subtitle())
            self._session_label.set_tooltip_text(self._session_subtitle(verbose=True))

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
                if first_text_chunk:
                    GLib.idle_add(self._mark_stream_started)
                    first_text_chunk = False
                GLib.idle_add(self._append_chunk, chunk)
        except Exception as e:
            if not self._alive:
                return
            log.exception("turn failed: %s", e)
            GLib.idle_add(self._append_chunk, f"\n\n*error: {e}*\n")
        finally:
            log.info("run_turn: end streamed=%s", self._stream_started)
            if self._alive:
                GLib.idle_add(self._mark_idle)

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
        self._streaming = False
        self._stream_started = False
        self._turn_cancelled = False
        self._active_assistant = None
        if self._alive:
            # Compose was never disabled; just reclaim focus so the user
            # can continue typing without clicking. Queued items stay
            # put — user controls when each one goes via the card's ⏎.
            self._compose.focus()
            self._update_phase()
            if streamed:
                self._notify_finished()

        return False

    def _mark_stream_started(self) -> bool:
        """Main-thread sink: first reply chunk landed, flip pending →
        streaming (unless we're currently awaiting approval / an answer,
        in which case `_update_phase` keeps the blue awaiting pill)."""
        self._stream_started = True
        self._update_phase()

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
        # Manual-drain policy: ⏎ dispatches this specific card only if
        # nothing is currently streaming. While streaming, the button is
        # a soft no-op — the user can wait or use the ⏎ on another
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
        if call.name and self._permission.decide(call.name) is not None:
            log.debug("tool %r short-circuited by permission state", call.name)
            return False
        row = PermissionRow(
            call,
            on_allow=self._on_permission_allow,
            on_trust=self._on_permission_trust,
            on_deny=self._on_permission_deny,
            on_auto_reject=self._on_permission_auto_reject,
        )
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
        self._notify_approval(call.name)

        return False

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
        from lib.acp_adapter import select_option_id  # lazy import

        auto_kind = self._permission.decide(call.name or "")
        if auto_kind is not None:
            resolve(select_option_id(options, auto_kind))
            return False

        def on_allow(r: PermissionRow) -> None:
            self._remove_permission_row(r)
            resolve(select_option_id(options, "allow_once"))

        def on_trust(r: PermissionRow) -> None:
            tool_name = r.tool_name
            if tool_name:
                self._permission.trust(tool_name)
                self._sync_permission_state()
            for existing in list(self._permissions):
                if existing.tool_name == tool_name:
                    self._remove_permission_row(existing)
            resolve(select_option_id(options, "allow_always"))

        def on_deny(r: PermissionRow) -> None:
            self._remove_permission_row(r)
            resolve(select_option_id(options, "reject_once"))

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
            resolve(select_option_id(options, "reject_always"))

        row = PermissionRow(
            call,
            on_allow=on_allow,
            on_trust=on_trust,
            on_deny=on_deny,
            on_auto_reject=on_auto_reject,
        )
        was_empty = not self._permissions
        self._permissions.append(row)
        self._permissions_listbox.append(row)
        self._permissions_box.set_visible(True)
        self._update_phase()
        if was_empty:
            GLib.idle_add(row.focus_allow)
        self._notify_approval(call.name)

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
        resource_open = self._palette is not None and self._palette.is_open()
        permissions_open = (
            self._permissions_palette is not None
            and self._permissions_palette.is_open()
        )
        mcp_open = self._mcp_palette is not None and self._mcp_palette.is_open()
        sessions_open = (
            self._sessions_palette is not None and self._sessions_palette.is_open()
        )
        if ctrl and keyval == Gdk.KEY_space:
            if resource_open:
                self._palette.close()
            else:
                self._open_resource_palette()
            return True
        if ctrl and keyval == Gdk.KEY_k:
            if permissions_open:
                self._permissions_palette.close()
            else:
                self._open_permissions_palette()
            return True
        if ctrl and keyval == Gdk.KEY_m:
            if mcp_open:
                self._mcp_palette.close()
            else:
                self._open_mcp_palette()
            return True
        if ctrl and keyval == Gdk.KEY_s:
            if sessions_open:
                self._sessions_palette.close()
            else:
                self._open_sessions_palette()
            return True
        # Esc when any palette is open should dismiss only the palette.
        any_palette_open = (
            resource_open or permissions_open or mcp_open or sessions_open
        )
        if any_palette_open and keyval == Gdk.KEY_Escape:
            return False
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
        """Short human label for the pill. Images say `📎 image/png` with
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
            return f"📎 {mime} · {size_str}"
        if att.uri:
            return f"📎 {mime} · {att.uri}"
        return f"📎 {mime}"

    def _on_pending_attachment_remove(self, key: object) -> None:
        self._pending_attachments = [
            att for att in self._pending_attachments if att is not key
        ]
        self._refresh_attachment_pills()

    def _open_resource_palette(self) -> None:
        """Ctrl+Space: raise the resource palette over the compose area
        with a freshly-collected resource list. Lazy-constructs the
        palette on first call, re-uses the same widget on subsequent
        opens — state (search input, active toggles) resets every
        `open()` call so stale ticks don't leak across sessions.

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
        """Ctrl+K: raise a palette listing every trusted / auto-
        approved / auto-rejected tool. Tab ticks rows, Enter drops
        each ticked tool from its bucket. Exists because the compose-
        bar pill strip overflows past a dozen or so entries — a
        filterable list scales better and keeps the compose area
        clear."""
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
        """Ctrl+M: view-only palette listing every MCP server the
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
        """Ctrl+S: palette listing known ACP sessions the adapter can
        resume. Commit replays the picked session — pilot asks the
        adapter to tear down the current session and re-bind to the
        chosen id. Gracefully degrades to the current session only
        when the adapter can't enumerate."""
        if self._sessions_palette is None:
            self._sessions_palette = CommandPalette(
                host_overlay=self._compose_overlay,
                on_commit=self._commit_session_restore,
                on_cancel=self._compose.focus,
                placeholder=("Switch session — Enter restores · Esc cancels"),
            )
        self._size_palette(self._sessions_palette)
        self._sessions_palette.preseed_active(set())
        self._sessions_palette.open(self._collect_session_entries())

    def _collect_session_entries(self) -> list[tuple[str, str, str, str]]:
        """Build the (kind, id, description, preview) list for the
        sessions palette. The adapter exposes the current session id
        via `_session._session_id`; listing all sessions isn't on the
        ACP surface so we show only the current one for now, plus a
        `new` sentinel that starts fresh on commit."""
        out: list[tuple[str, str, str, str]] = []
        session = getattr(self._adapter, "_session", None)
        current = getattr(session, "_session_id", None)
        if current:
            out.append(
                ("session", current, "current session · press Enter to keep", current)
            )
        out.append(("new-session", "new", "start a fresh ACP session", "new"))
        return out

    def _commit_session_restore(self, entries) -> None:
        """Commit handler for the Ctrl+S sessions palette. Only the
        `new-session` sentinel has a real effect today — it tears the
        adapter down, freeing the agent subprocess for a clean start.
        Existing-session picks are a no-op (we're already on the
        current session)."""
        for kind, _name, _desc, _preview in entries:
            if kind == "new-session":
                try:
                    self._adapter.close()
                except Exception as e:
                    log.warning("adapter close raised: %s", e)
                self._active_assistant = None
                break
        self._compose.focus()

    def _collect_resources(self) -> list[tuple[str, str, str, str]]:
        """Build the `(kind, name, description, preview)` list feeding
        the Ctrl+Space palette — skills only. Sourced via our own MCP
        server's `resources/list` so the palette and the agent see the
        same set."""
        resources: list[tuple[str, str, str, str]] = []
        if self._skills_dir:
            for skill in list_skills_via_mcp(
                _PILOT_MCP_SCRIPT, skills_dir=self._skills_dir
            ):
                resources.append(("skill", skill.name, skill.description, skill.uri))
        return resources

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
        """Ctrl+G: click the `✓ allow` button on the oldest pending
        permission row. Keyboard-only accept for the row that grabbed
        focus when it appeared — saves the user a Tab-to-allow + Enter
        dance when they just want to approve and move on. Silent no-op
        when the panel is empty."""
        if not self._permissions:
            return
        self._permissions[0]._allow_btn.emit("clicked")

    def _reject_first_permission(self) -> None:
        """Ctrl+R: click the `✕ deny` button on the oldest pending
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
        can't push the compose box off-screen."""
        if monitor is not None:
            self._compose.set_max_content_fraction(monitor.get_geometry().height, 0.25)

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

def _is_live() -> bool:
    """Probe the session socket without sending a command. Returns True if
    a server accepted our connect, False if the file is stale / absent."""
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(1)
    try:
        probe.connect(_PATHS.socket_path)

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
        sock.connect(_PATHS.socket_path)
    except (FileNotFoundError, ConnectionRefusedError):
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

class Session:
    """Owns the Unix socket for a live pilot window. A background thread
    accepts connections from forwarder invocations and dispatches their
    `turn` / `status` commands back onto the GTK main thread."""

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
            if _is_live():
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
                    # If the overlay is on-screen, STAGE the new text
                    # into the compose area so the user gets an Enter
                    # confirmation before the turn goes out. This is
                    # the right UX for speech press-2 / follow-up
                    # forwards arriving against a live, visible pilot
                    # — the user sees what was picked up and can edit
                    # / discard before submitting. When the overlay is
                    # hidden the old dispatch-immediately path fires
                    # instead (fire-and-forget speech flow keeps
                    # working, and `dispatch_turn` will re-show the
                    # window before running the turn).
                    if self._window.get_visible():
                        GLib.idle_add(self._window.stage_turn, text)
                    else:
                        GLib.idle_add(self._window.dispatch_turn, text)

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
                    "session_store_path": (
                        getattr(adapter, "session_store_path", None) or ""
                    ),
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

def _build_pilot_mcp_server(skills_dir: Optional[str]) -> "McpServerStdio":
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
        # "awaiting approval, tool hasn't executed yet".
        tool_call = _ToolCall(
            tool_id=call.tool_id,
            name=call.name,
            arguments=call.arguments,
            status="running",
            audit=False,
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

def _default_mcp_names() -> list[str]:
    """Full MCP catalog used when `--mcp` is omitted. `system` first
    (pilot's own server, always ships), then every external entry the
    mcphub JSON produced."""
    return [_PILOT_MCP_SERVER_NAME, *DEFAULT_SERVER_NAMES]

def _resolve_mcp(args) -> list[str]:
    """De-dupe `--mcp NAME` flags into an ordered list. Each flag
    value may itself be a comma- or whitespace-separated list —
    `--mcp git,memory` and `--mcp git --mcp memory` produce identical
    output. Omitting the flag falls through to the full catalog;
    `--mcp ""` disables everything. Unknown names survive to
    `_acp_mcp_servers` which logs and skips."""
    raw_values = getattr(args, "mcp", None)
    if raw_values is None:
        return _default_mcp_names()
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
    """Return the AGENTS.md contents (or empty string if missing / not
    configured). Any read error degrades to "" + a warning — we never
    block `toggle` on a missing bootstrap file."""
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

def _compose_system_prompt(base: str, agents_md: str) -> str:
    """Prepend `agents_md` to `base`, separated by a blank line, iff
    `agents_md` has content. Otherwise return `base` unchanged so we
    don't introduce a leading newline into the default prompt."""
    if not agents_md:
        return base

    return f"{agents_md}\n\n{base}"

def _build_adapter(args) -> ConversationAdapter:
    provider = ConversationProvider(args.converse_provider)
    cwd = getattr(args, "cwd", None)
    agents_md_path = getattr(args, "agents_md", None)
    skills_dir = os.path.expanduser(getattr(args, "skills_dir", "") or "") or None
    mcp_servers = _acp_mcp_servers(
        mcp=_resolve_mcp(args),
        skills_dir=skills_dir,
    )
    system_prompt = _compose_system_prompt(
        AI_SYSTEM_PROMPT,
        _read_agents_md(agents_md_path),
    )
    session_store_path = _session_store_path(
        suffix=_PATHS.suffix,
        provider=provider,
        model=args.converse_model,
        cwd=cwd,
    )
    log.info(
        "_build_adapter: provider=%s model=%s cwd=%s session_store=%s",
        provider.value,
        args.converse_model,
        cwd,
        session_store_path,
    )
    match provider:
        case ConversationProvider.CLAUDE:
            return ConversationAdapterClaude(
                system_prompt,
                model=args.converse_model,
                cwd=cwd,
                mcp_servers=mcp_servers,
                session_store_path=session_store_path,
            )
        case ConversationProvider.OPENCODE:
            return ConversationAdapterOpenCode(
                system_prompt,
                model=args.converse_model,
                cwd=cwd,
                mcp_servers=mcp_servers,
                session_store_path=session_store_path,
            )
        case _:
            raise ValueError(f"unknown converse provider: {provider!r}")

_MODEL_TAG_RE = re.compile(r"[^a-z0-9]+")

def _model_tag(model: Optional[str]) -> str:
    """Slugify `--converse-model` into a filesystem-safe token so it can
    ride in the session-store filename. `glm-5.1:cloud` → `glm-5-1-cloud`,
    None / empty → `default`. Keeps store paths predictable for the
    `forget` / `session-info` commands."""
    if not model:
        return "default"
    slug = _MODEL_TAG_RE.sub("-", model.lower()).strip("-")
    return slug or "default"

def _session_store_path(
    *,
    suffix: str,
    provider: "ConversationProvider",
    model: Optional[str],
    cwd: Optional[str],
) -> str:
    """Derive the on-disk path where the ACP `session_id` for this
    (suffix, provider, model, cwd) quadruple is persisted. Kept under
    `$XDG_STATE_HOME/pilot/sessions/` so uninstalling pilot cleans up
    with the rest of user state.

    The key encodes:
      - `suffix` — `--session` flag (e.g. "plan"); scopes sessions per
        overlay so "plan" and "ask" don't collide.
      - `provider` — Claude and OpenCode sessions aren't
        interchangeable; different agents store different ids.
      - `model` — resumed sessions keep whichever model they were
        created with (opencode / claude-agent-acp don't reapply
        `--model` to `load_session`). Including the model in the key
        makes `--converse-model glm-5.1:cloud` vs `sonnet` spawn
        *distinct* stored sessions so changing the flag actually
        changes the model.
      - `cwd` — the same `--session plan` launched against `~/notes`
        vs `~/work` should resume INTO the corresponding project;
        hashing cwd into the key splits them cleanly.

    The cwd is hashed rather than path-embedded so filesystem-unsafe
    characters in long paths (colons, slashes) don't leak into the
    filename."""
    import hashlib

    state_home = os.environ.get("XDG_STATE_HOME") or os.path.expanduser(
        "~/.local/state"
    )
    root = os.path.join(state_home, "pilot", "sessions")
    suffix_tag = suffix or "default"
    cwd_key = cwd or os.getcwd()
    cwd_hash = hashlib.sha1(cwd_key.encode("utf-8")).hexdigest()[:10]
    filename = f"{suffix_tag}-{provider.value}-{_model_tag(model)}-{cwd_hash}.session"
    return os.path.join(root, filename)

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
    # stdin being a TTY means the user invoked pilot.py from a terminal
    # or a compositor bind with nothing piped in; a closed/empty pipe
    # means the upstream process exited without writing (common with
    # press-2 of `speech.py toggle --output stdout | pilot.py toggle`).
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
    / streaming yellow / awaiting blue); queue depth renders as a Pango
    superscript badge so N pending turns show as `󱍊³` without stealing
    horizontal space."""
    resp = _send("status")
    if not resp or not resp.get("ok"):
        print(json.dumps({"class": "idle", "text": "", "tooltip": "Pilot idle"}))

        return

    provider = resp.get("provider", "")
    phase = resp.get("phase", "idle")
    queue = int(resp.get("queue", 0) or 0)
    session = resp.get("session") or _PATHS.suffix
    session_tag = f" ({session})" if session else ""
    icon = "󱍊"
    badge = f"<sup>{queue}</sup>" if queue > 0 else ""
    text = f"{icon}{session_tag}{badge}"
    label = f"Pilot{' ' + session_tag if session_tag else ''}"
    match phase:
        case "streaming":
            tooltip = f"{label}: streaming via {provider}"
        case "pending":
            tooltip = f"{label}: waiting on first chunk from {provider}"
        case "awaiting":
            tooltip = f"{label}: waiting on user input ({provider})"
        case _:
            tooltip = f"{label}: {provider} idle"
    if queue > 0:
        tooltip += f"  ({queue} queued)"
    print(json.dumps({"class": phase, "text": text, "tooltip": tooltip}))

def _cmd_is_running() -> None:
    """Waybar `exec-if` gate. Exit 0 when a session socket is live so the
    custom module shows, otherwise exit 1 and stay hidden."""
    sys.exit(0 if _is_live() else 1)

def _cmd_kill() -> None:
    """End the running pilot session (if any) and return immediately. Matches
    `speech.py kill` so recording-mode bindings can terminate either."""
    resp = _send("kill")
    if not resp:
        # No live session answered — clear a stale socket file so the next
        # toggle starts clean.
        try:
            os.unlink(_PATHS.socket_path)
        except FileNotFoundError:
            pass
    _signal_waybar_safe()

def _cmd_forget(args) -> None:
    """Delete the stored ACP session_id for the (suffix, provider, model,
    cwd) slot so the next `toggle` creates a fresh conversation. This only
    removes pilot's pointer file — the agent (opencode / claude-agent-acp)
    keeps its own on-disk session record untouched; changing that is the
    agent's job, not pilot's.

    Writes a one-line result to stdout so shell wrappers can tell whether
    anything was actually cleared."""
    provider = ConversationProvider(args.converse_provider)
    cwd = args.cwd
    if cwd:
        cwd = os.path.expanduser(cwd)
    path = _session_store_path(
        suffix=_PATHS.suffix,
        provider=provider,
        model=args.converse_model,
        cwd=cwd,
    )
    try:
        os.unlink(path)
        print(f"forgot {path}")
    except FileNotFoundError:
        print(f"nothing to forget at {path}")
    except OSError as e:
        print(f"forget failed ({path}): {e}", file=sys.stderr)
        sys.exit(1)

def _cmd_session_info() -> None:
    """Emit a JSON snapshot of the session slot on stdout. Includes the
    live session's `status` response when a pilot is running AND the
    on-disk store contents even when nothing is running — so users can
    verify that restart-resume actually lines up.

    Read-only; never touches the store."""
    # Live snapshot: whatever the running pilot is willing to report.
    live = _send("status") or {}
    # Disk snapshot: scan the sessions directory for any file that
    # carries our suffix prefix. Several entries may exist per suffix
    # when the user has cycled through provider / model combos.
    state_home = os.environ.get("XDG_STATE_HOME") or os.path.expanduser(
        "~/.local/state"
    )
    sessions_dir = os.path.join(state_home, "pilot", "sessions")
    suffix_tag = _PATHS.suffix or "default"
    stored: list[dict] = []
    try:
        entries = sorted(os.listdir(sessions_dir))
    except FileNotFoundError:
        entries = []
    for entry in entries:
        if not entry.startswith(f"{suffix_tag}-") or not entry.endswith(".session"):
            continue
        full = os.path.join(sessions_dir, entry)
        try:
            with open(full, "r", encoding="utf-8") as f:
                sid = f.read().strip()
        except OSError:
            sid = ""
        stored.append({"path": full, "session_id": sid})
    print(
        json.dumps(
            {
                "suffix": _PATHS.suffix,
                "live": live,
                "stored": stored,
                "sessions_dir": sessions_dir,
            },
            indent=2,
        )
    )
    _signal_waybar_safe()

def main():
    parser = argparse.ArgumentParser(description="Conversational AI sidebar overlay")
    parser.add_argument("-v", "--verbose", action="store_true")
    # Session suffix: when set, rewrites every user-visible runtime path
    # (main socket, MCP socket, adapter config files) plus the GTK
    # app-id to include `-<suffix>`. Lets multiple pilot overlays coexist
    # (e.g. the default "ask" pilot and a dedicated "plan" pilot). Empty
    # string keeps the shipped paths byte-for-byte identical, so no-flag
    # behaviour is unchanged.
    parser.add_argument(
        "--session",
        default="",
        metavar="SUFFIX",
        help=(
            "session suffix — appended to socket / config-file / app-id "
            "names so multiple pilot overlays can coexist. Empty (default) "
            "keeps the original paths."
        ),
    )
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
    # None → per-adapter default (`sonnet` for Claude, whatever
    # opencode picks for OpenCode).
    toggle_parser.add_argument("--converse-model", default=None)
    # Working directory for the spawned ACP agent subprocess. Default
    # = a fresh `mkdtemp` so each session runs in a clean-room sandbox.
    toggle_parser.add_argument(
        "--cwd",
        default=None,
        metavar="PATH",
        help=(
            "working directory for the spawned AI CLI. Defaults to a "
            "fresh tempdir (tempfile.mkdtemp(prefix='pilot-'))."
        ),
    )
    # Auto-approve: tool names whose ACP permission requests are
    # short-circuited to `allow` without surfacing a row in the
    # overlay. Repeatable, matches case-insensitively and treats
    # `-` / `_` as equivalent (so `Read`, `read`, `read-file`,
    # `read_file` all canonicalise the same). Rendered as green pills
    # on the submit bar; click a pill to drop the tool back into the
    # normal approval flow.
    toggle_parser.add_argument(
        "--auto-approve",
        action="append",
        dest="auto_approve",
        default=[],
        metavar="TOOL_NAME",
        help=(
            "Tool name to auto-approve without prompting the user. "
            "Repeatable; case-insensitive, `-`/`_` unified. Click the "
            "corresponding green pill in the compose bar to revoke."
        ),
    )
    # Auto-reject: mirror of `--auto-approve` — tool names whose
    # permission requests short-circuit to `deny`. Repeatable, same
    # normalisation rules. Rendered as red pills; click to drop the
    # tool back into the normal approval flow.
    toggle_parser.add_argument(
        "--auto-reject",
        action="append",
        dest="auto_reject",
        default=[],
        metavar="TOOL_NAME",
        help=(
            "Tool name to auto-reject without prompting the user. "
            "Repeatable; case-insensitive, `-`/`_` unified. Click the "
            "corresponding red pill in the compose bar to revoke."
        ),
    )
    # Bootstrap-rules injection. When set (and the file exists) the
    # contents are PREPENDED to the provider's system prompt on the
    # first turn, AND registered as `mcp__pilot__resource__agents` so
    # the model can re-read the canonical source mid-session. Default
    # matches the nvim config's AGENTS.md path; set to empty string to
    # disable injection entirely.
    toggle_parser.add_argument(
        "--agents-md",
        dest="agents_md",
        default="~/.config/nvim/utils/agents/AGENTS.md",
        metavar="PATH",
        help=(
            "Path to AGENTS.md. When the file exists its contents are "
            "prepended to the system prompt and exposed as an MCP "
            "resource tool so the model can re-read it on demand. "
            "Empty string disables injection. "
            "Default: ~/.config/nvim/utils/agents/AGENTS.md"
        ),
    )
    # Skills directory. When set (and the dir exists) the subprocess
    # registers `list_skills` + `load_skill` MCP tools backed by the
    # `*/SKILL.md` layout from mcphub-nvim. Default matches the nvim
    # config; set to "" to skip.
    toggle_parser.add_argument(
        "--skills-dir",
        dest="skills_dir",
        default="~/.config/nvim/utils/agents/skills",
        metavar="DIR",
        help=(
            "Path to the agent skills root. Each subdirectory must "
            "contain a SKILL.md with `---`-delimited YAML frontmatter "
            "(name + description). Empty string disables the skills "
            "MCP capability. "
        ),
    )
    # MCP servers to register alongside pilot. Repeatable; each value
    # may itself be comma- or whitespace-separated so shell helpers
    # can pass bulk lists (`--mcp git,memory`). Names resolve against
    # `lib.mcp_servers.DEFAULT_SERVERS` (nvim form like `argocd/kilic`
    # accepted too). Omitting the flag entirely picks the full
    # catalog; pass `--mcp ""` explicitly to opt everything out.
    toggle_parser.add_argument(
        "--mcp",
        action="append",
        dest="mcp",
        default=None,
        metavar="NAME",
        help=(
            "MCP server to register. Repeatable; accepts comma / "
            "whitespace-separated lists. Omit to enable the full "
            "catalog; pass an empty value to disable."
        ),
    )

    subparsers.add_parser("status", help="print waybar-shaped JSON status")
    subparsers.add_parser(
        "is-running",
        help="exit 0 if a session is live, non-zero otherwise",
    )
    subparsers.add_parser("kill", help="terminate the running session (if any)")
    forget_parser = subparsers.add_parser(
        "forget",
        help=(
            "drop the stored ACP session_id for a (session/provider/model/cwd) "
            "slot so the next toggle starts a brand-new conversation. Does NOT "
            "touch the agent's own on-disk session store — opencode / "
            "claude-agent-acp keep their records, pilot just stops pointing "
            "at them."
        ),
    )
    # forget mirrors the subset of toggle flags that feed into the store
    # key, so `pilot --session plan forget --converse-provider opencode
    # --converse-model glm-5.1:cloud --cwd ~/notes` resolves the EXACT
    # path that the matching toggle would write.
    forget_parser.add_argument(
        "--converse-provider",
        choices=list(ConversationProvider),
        default=DEFAULT_CONVERSE_ADAPTER,
    )
    forget_parser.add_argument("--converse-model", default=None)
    forget_parser.add_argument("--cwd", default=None)

    subparsers.add_parser(
        "session-info",
        help=(
            "print the session state — live status (if any), store path, "
            "stored session_id — as JSON on stdout. Read-only."
        ),
    )

    args = parser.parse_args()

    # Always log to stderr — stdout belongs to waybar-style status
    # subcommands (`status` emits JSON) and to any future pipe consumer.
    # INFO default so the key event-points (spawn, session-id, prompt
    # shape, permission round-trips, MCP server chatter) surface
    # without needing `-v`; `-v` bumps to DEBUG which adds per-chunk
    # and per-RPC detail.
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        level=logging.DEBUG if args.verbose else logging.INFO,
        stream=sys.stderr,
    )

    global _PATHS
    _PATHS = PilotPaths.from_suffix(args.session or "")

    # Expand `~` in user-supplied `--cwd`. The None → mkdtemp default is
    # deferred into `_cmd_toggle` so status / kill / forwarding turns
    # never leak a stray tempdir.
    if args.command == "toggle" and args.cwd:
        args.cwd = os.path.expanduser(args.cwd)

    match args.command:
        case "toggle":
            _cmd_toggle(args)
        case "status":
            _cmd_status()
        case "is-running":
            _cmd_is_running()
        case "kill":
            _cmd_kill()
        case "forget":
            _cmd_forget(args)
        case "session-info":
            _cmd_session_info()

if __name__ == "__main__":
    main()
