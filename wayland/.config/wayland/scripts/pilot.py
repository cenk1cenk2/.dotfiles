#!/usr/bin/env python3
"""pilot — GTK4 layer-shell sidebar that streams a conversational AI response.

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
import tempfile
import threading
from typing import Any, Callable, Optional

from markdown_it import MarkdownIt

from lib import (
    DEFAULT_CONVERSE_ADAPTER,
    DEFAULT_SERVER_NAMES,
    ConversationAdapter,
    ConversationAdapterClaude,
    ConversationAdapterCodex,
    ConversationAdapterHttp,
    ConversationProvider,
    ConversationAdapterOpenCode,
    InputAdapterClipboard,
    InputAdapterStdin,
    InputMode,
    McpConfig,
    OutputAdapterClipboard,
    ThinkingChunk,
    ToolCall,
    format_tool_args,
    get_server as _DEFAULT_SERVER_GET,
    highlight_code,
    load_prompt,
    load_relative_file,
    notify,
    signal_waybar,
)
from lib.converse import _deep_merge

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

# Very-early MCP server mode. Claude spawns us as a stdio MCP server
# via `--mcp-config` → our config points back at this same script with
# `mcp-server` as argv[1]. We don't want to drag gtk4-layer-shell /
# GTK imports into that subprocess (it has no display + doesn't need
# them), so we short-circuit to the MCP event loop here and exit.
def _early_session_suffix() -> str:
    """Extract `--session <value>` / `--session=<value>` from sys.argv
    without running argparse. Needed for the early mcp-server branch at
    the top of the file, which can't build a full parser (the GTK /
    layer-shell imports below haven't run yet and the subparsers aren't
    defined). Returns "" when the flag isn't present."""
    argv = sys.argv[1:]
    i = 0
    while i < len(argv):
        token = argv[i]
        if token == "--session" and i + 1 < len(argv):
            return argv[i + 1]
        if token.startswith("--session="):
            return token.split("=", 1)[1]
        i += 1

    return ""

def _early_repeat_flag(flag: str) -> list[str]:
    """Pull every `--<flag> <value>` / `--<flag>=<value>` occurrence from
    sys.argv without touching argparse. Mirrors `_early_session_suffix`
    but for repeatable flags like `--auto-approve` / `--auto-reject`
    that the early mcp-server branch needs to honour before the main
    CLI parser exists."""
    prefix_equals = f"{flag}="
    out: list[str] = []
    argv = sys.argv[1:]
    i = 0
    while i < len(argv):
        token = argv[i]
        if token == flag and i + 1 < len(argv):
            out.append(argv[i + 1])
            i += 2
            continue
        if token.startswith(prefix_equals):
            out.append(token.split("=", 1)[1])
        i += 1

    return out

def _mcp_sock_name(suffix: str) -> str:
    """Return the runtime-relative socket filename for the MCP bridge.
    With no suffix that's `wayland-pilot-mcp.sock`; with suffix `plan`
    it's `wayland-pilot-mcp-plan.sock`. Centralised so both the early
    mcp-server branch and the main-path session plumbing agree."""
    return f"wayland-pilot-mcp-{suffix}.sock" if suffix else "wayland-pilot-mcp.sock"

if len(sys.argv) > 1 and sys.argv[1] == "mcp-server":
    # Late import so the normal pilot.py paths still load gi below.
    from lib.mcp import (  # noqa: E402
        McpCapability,
        McpServer,
        question_route,
        socket_approval,
        socket_auto_check,
        socket_question,
    )

    _runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    _mcp_sock = os.path.join(_runtime, _mcp_sock_name(_early_session_suffix()))
    _server = McpServer("pilot")
    # Seed the subprocess's local auto sets from CLI args. These serve
    # as the offline-mode source of truth when the bridge socket is
    # unreachable; when it IS reachable the socket_auto_check route
    # (installed first below) consults the overlay's live sets instead,
    # so user-driven mutations (pill clicks, ⛔ auto-reject button)
    # take effect mid-session without having to rebuild the subprocess.
    _server.add_auto_approve(*_early_repeat_flag("--auto-approve"))
    _server.add_auto_reject(*_early_repeat_flag("--auto-reject"))
    # Install the socket-backed auto-check route BEFORE anything else —
    # the overlay is the authoritative source for live auto-list
    # membership. `continue` responses / transport errors fall through
    # to the server's local auto-list route (seeded above), then the
    # question route, then the base socket_approval callback.
    _server.prepend_approval_route(*socket_auto_check(_mcp_sock))
    # Single question callback shared between two entry points: the
    # generic approval router (registered below via
    # `add_approval_route`) pivots the model's built-in
    # `AskUserQuestion`-style tools onto it, AND the standalone
    # `ask_question` MCP tool uses the same callback when the model
    # explicitly picks our tool.
    _question_cb = socket_question(_mcp_sock)
    _server.enable(McpCapability.APPROVAL, callback=socket_approval(_mcp_sock))
    _server.add_approval_route(*question_route(_question_cb))
    # Extra routes can be appended here — each is a (matcher, handler)
    # pair that runs before the base approval callback:
    #   _server.add_approval_route(
    #       matcher=lambda tool, inp: tool == "ExitPlanMode",
    #       handler=lambda tool, inp: {"behavior": "allow", "updatedInput": inp},
    #   )
    _server.enable(McpCapability.QUESTION, callback=_question_cb)
    # `mcp__pilot__open` hands URIs / file paths to `xdg-open` — lets the
    # AI ask to open things in the browser, obsidian, etc. The call
    # still goes through the approval router first, so the user sees a
    # row before anything spawns.
    _server.enable(McpCapability.OPEN)
    # Skills directory: one --skills-dir flag enables the list/load tools
    # backed by that directory. Parsed via the same early-flag trick as
    # the auto-approve / auto-reject lists — argparse hasn't run yet.
    for _skills_dir in _early_repeat_flag("--skills-dir"):
        if not _skills_dir:
            continue
        _server.enable(McpCapability.SKILLS, skills_dir=_skills_dir)
        break  # only one skills dir per subprocess; later flags ignored
    # Agents.md (and any other file-backed resources): each --agents-md
    # registers `resource__agents` pointing at the given path. If more
    # bootstrap files ever need the same treatment we can splat more
    # `--resource name=path` flags through here.
    for _agents_path in _early_repeat_flag("--agents-md"):
        if not _agents_path:
            continue
        _server.enable(
            McpCapability.RESOURCE,
            name="agents",
            path=_agents_path,
            description=(
                "Fetch the AGENTS.md bootstrap rules — read this at the "
                "start of the session to load project conventions, MCP "
                "discovery steps, and long-standing context."
            ),
        )
        break  # single agents.md per subprocess
    _server.run()
    sys.exit(0)

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

log = logging.getLogger("pilot")

APP_ID = "dev.kilic.wayland.pilot"
_RUNTIME = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
# Main CLI socket — toggle/status/kill/turn from user-facing invocations.
SOCKET_PATH = os.path.join(_RUNTIME, "wayland-pilot.sock")
# Dedicated MCP permission-prompt socket. The pilot MCP subprocess connects
# here and blocks on overlay approval before returning allow/deny to claude.
# Split from the main socket so a user kill / stale-cleanup never trips the
# MCP bridge (and vice-versa).
MCP_SOCKET_PATH = os.path.join(_RUNTIME, "wayland-pilot-mcp.sock")
# Session suffix applied at startup via `_apply_session_suffix` when
# `--session <name>` is passed. Empty string keeps the original paths /
# app-id verbatim so no-flag behaviour matches exactly what shipped
# before. We track the bare suffix separately (not baked into the
# globals) so adapter-config files can reuse it.
SESSION_SUFFIX: str = ""

def _apply_session_suffix(suffix: str) -> None:
    """Rewrite the module-level socket paths + app-id to include
    `-<suffix>` before the extension. Called from `main()` after
    parsing `--session`; with an empty suffix the globals keep the
    shipped defaults exactly."""
    global APP_ID, SOCKET_PATH, MCP_SOCKET_PATH, SESSION_SUFFIX
    SESSION_SUFFIX = suffix or ""
    if not suffix:
        return
    APP_ID = f"dev.kilic.wayland.pilot.{suffix}"
    SOCKET_PATH = os.path.join(_RUNTIME, f"wayland-pilot-{suffix}.sock")
    MCP_SOCKET_PATH = os.path.join(_RUNTIME, _mcp_sock_name(suffix))

AI_SYSTEM_PROMPT = load_prompt("pilot.md", relative_to=__file__)

def _signal_waybar_safe() -> None:
    """Nudge waybar's `custom/pilot` module to re-read status. Non-fatal —
    waybar-signal.sh silently ignores unknown modules, and we shouldn't
    let waybar being unavailable take down the overlay."""
    try:
        signal_waybar("pilot")
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
                # `tok.info` on a fence is the language string after the
                # opening triple-backticks (e.g. ```python foo → "python
                # foo"). We take the first whitespace-delimited word and
                # let `highlight_code` pygments-dispatch from there.
                # `code_block` (indented) has no language hint at all —
                # None triggers pygments' `guess_lexer` fallback.
                info = (getattr(tok, "info", "") or "").strip().split()
                language = info[0] if info else None
                highlighted = highlight_code(content, language)
                out.append(
                    f'<span font_family="monospace" '
                    f'background="{self.CODE_BG}">'
                    f"{highlighted}</span>\n\n"
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
                out.append('<span foreground="#4b5263">' + ("─" * 40) + "</span>\n\n")
            case _:
                log.debug("unhandled token: %s", tok.type)

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
    MIN_ROWS = 5

    def __init__(self, on_submit=None):
        self._on_submit = on_submit

        self.widget = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.widget.add_css_class("pilot-compose-wrap")

        # Question banner. Hidden until `set_question_mode` is called
        # by a claude `ask_question` MCP forward; shows the question
        # text above the compose textview with a "✕ skip" button. The
        # next submit sends its text to the question callback instead
        # of dispatching a normal turn; the banner clears as soon as
        # the user answers or skips.
        self._question_banner = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
        )
        self._question_banner.add_css_class("pilot-question-banner")
        self._question_label = Gtk.Label(
            xalign=0.0,
            hexpand=True,
            wrap=True,
            wrap_mode=Pango.WrapMode.WORD_CHAR,
            selectable=True,
        )
        self._question_label.add_css_class("pilot-question-text")
        self._question_banner.append(self._question_label)
        skip_btn = Gtk.Button(label="✕ skip")
        skip_btn.add_css_class("pilot-question-skip")
        skip_btn.set_tooltip_text("Skip this question — return an empty answer")
        skip_btn.connect("clicked", lambda _b: self._on_question_skip())
        self._question_banner.append(skip_btn)
        self._question_banner.set_visible(False)
        self.widget.append(self._question_banner)
        self._question_callback: Optional[Callable[[str], None]] = None

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

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar.add_css_class("pilot-compose-bar")

        # Trusted-tool pills live on the left of the submit bar. Each
        # pill is a single button labelled `name ✕`; clicking invokes
        # the registered remove callback (set from PilotWindow). Hidden
        # until at least one tool is trusted. Using a FlowBox so we
        # wrap gracefully if the user has trusted a lot of tools.
        self._pills_flow = Gtk.FlowBox(
            orientation=Gtk.Orientation.HORIZONTAL,
            column_spacing=4,
            row_spacing=4,
            hexpand=True,
            valign=Gtk.Align.CENTER,
        )
        self._pills_flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self._pills_flow.set_max_children_per_line(64)
        self._pills_flow.set_homogeneous(False)
        self._pills_flow.add_css_class("pilot-compose-pills")
        self._pills_flow.set_visible(False)
        bar.append(self._pills_flow)
        self._pill_remove_cb = None

        hint = Gtk.Label(
            label="Enter · Shift+Enter newline · Ctrl+D interrupt · Ctrl+G accept · Ctrl+R reject · Ctrl+T thinking · Ctrl+P paste · Ctrl+Y yank · Ctrl+F focus · ESC hide · Ctrl+Q quit",
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

    def set_trusted_pills(self, names, on_remove) -> None:
        """Back-compat shim. Old callers pass just the trusted list and
        a 1-arg remove callback. Adapts to the new tri-list API by
        wrapping the callback to ignore the `kind` arg."""
        self.set_permission_pills(
            trusted=list(names),
            auto_approved=[],
            auto_rejected=[],
            on_remove=lambda name, _kind: on_remove(name),
        )

    def set_permission_pills(
        self,
        trusted,
        auto_approved,
        auto_rejected,
        on_remove,
    ) -> None:
        """Rebuild the compose-bar pill strip with three colour bands —
        accent (trusted), green (auto-approved), red (auto-rejected).
        `on_remove(name, kind)` gets the list the clicked pill came
        from so the window knows which set to drop it from
        (`"trusted"` / `"auto_approve"` / `"auto_reject"`).

        Hides the row when all three lists are empty; hides the hint
        label otherwise so pills + hint + send don't crowd the bar."""
        self._pill_remove_cb = on_remove
        # Clear the old children — FlowBox doesn't have a bulk-clear.
        child = self._pills_flow.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._pills_flow.remove(child)
            child = nxt

        groups = [
            ("trusted", list(trusted), None, "Click to untrust {name} (re-enables prompts)"),
            ("auto_approve", list(auto_approved), "auto-approve", "Click to drop {name} from the auto-approve list"),
            ("auto_reject", list(auto_rejected), "auto-reject", "Click to drop {name} from the auto-reject list"),
        ]
        total = sum(len(lst) for _k, lst, _cls, _tip in groups)
        if total == 0:
            self._pills_flow.set_visible(False)
            if self._hint_label is not None:
                self._hint_label.set_visible(True)

            return

        for kind, names, extra_cls, tooltip_fmt in groups:
            for name in names:
                btn = Gtk.Button(label=f"{name} ✕")
                btn.add_css_class("pilot-compose-pill")
                if extra_cls:
                    btn.add_css_class(extra_cls)
                btn.set_tooltip_text(tooltip_fmt.format(name=name))
                btn.connect(
                    "clicked",
                    lambda _b, n=name, k=kind: self._on_pill_click(n, k),
                )
                self._pills_flow.append(btn)
        self._pills_flow.set_visible(True)
        # Hide the hint when pills are present — pills + hint + send
        # would crowd the submit bar on narrow overlays.
        if self._hint_label is not None:
            self._hint_label.set_visible(False)

    def _on_pill_click(self, name: str, kind: str = "trusted") -> None:
        if self._pill_remove_cb is not None:
            # Accommodate both the new (name, kind) callback and any
            # legacy single-arg callers that slip through.
            try:
                self._pill_remove_cb(name, kind)
            except TypeError:
                self._pill_remove_cb(name)

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
        if not text:
            return
        # Question mode wins: while a claude `ask_question` is pending,
        # submit routes the typed text back to that callback instead of
        # dispatching a normal user turn. Clear the banner + callback
        # before firing so re-entrant calls don't double-answer.
        if self._question_callback is not None:
            cb = self._question_callback
            self.clear()
            self.clear_question_mode()
            cb(text)

            return
        if self._on_submit:
            self.clear()
            self._on_submit(text)

    def set_question_mode(
        self, question: str, on_answer: Callable[[str], None]
    ) -> None:
        """Show the question banner above the textview and wire the
        next submit to `on_answer(text)` instead of the normal turn
        dispatch. Replaces any previous question in flight — the prior
        callback is resolved with empty-string so the socket handler
        doesn't hang."""
        if (
            self._question_callback is not None
            and self._question_callback is not on_answer
        ):
            # Resolve the stale one so its socket thread unblocks.
            try:
                self._question_callback("")
            except Exception:
                log.exception("stale question callback raised")
        self._question_callback = on_answer
        self._question_label.set_label(f"🤔 {question}")
        self._question_banner.set_visible(True)

    def clear_question_mode(self) -> None:
        """Hide the banner and forget the callback. Safe to call when
        no question is active."""
        self._question_callback = None
        self._question_banner.set_visible(False)

    def has_question(self) -> bool:
        """True while the compose is in question-answer mode. Window
        reads this to include the banner in its awaiting-phase check."""
        return self._question_callback is not None

    def _on_question_skip(self) -> None:
        """`✕ skip` button: resolve the question with an empty answer
        and dismiss the banner without sending a turn."""
        cb = self._question_callback
        self.clear_question_mode()
        if cb is not None:
            try:
                cb("")
            except Exception:
                log.exception("question skip callback raised")

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
        self.add_css_class("pilot-queue-row")

        self._card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._card.add_css_class("pilot-queue-card")

        self._label = Gtk.Label(label=text, xalign=0.0, hexpand=True)
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
        scroller.add_css_class("pilot-queue-edit")

        textview = Gtk.TextView(
            wrap_mode=Gtk.WrapMode.WORD_CHAR,
            top_margin=6,
            bottom_margin=6,
            left_margin=8,
            right_margin=8,
        )
        textview.add_css_class("pilot-queue-edit-text")
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

    THINKING_LABEL_STREAMING = "🧠 thinking…"
    THINKING_LABEL_DONE = "🧠 thinking"

    # Per-status glyph on each tool bubble. Intentionally text-only so
    # these render at the card font size without Pango fighting an
    # inline `<tt>` or PangoAttrList.
    _TOOL_STATUS_GLYPHS = {
        "pending": "⋯",
        "running": "⋯",
        "completed": "✓",
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
        land live; `append()` closes it once the real reply starts."""
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
        self._thinking_text += chunk
        assert self._thinking_label is not None
        self._thinking_label.set_markup(self._md.render(self._thinking_text))

    def get_text(self) -> str:
        return self._text

    def toggle_thinking(self) -> bool:
        """Flip the thinking expander's open/closed state. Returns
        True if there was a thinking block to toggle, False otherwise
        — callers scanning for the latest thinking card use the
        return value to stop once they find one."""
        if self._thinking_expander is None:
            return False
        self._thinking_expander.set_expanded(
            not self._thinking_expander.get_expanded()
        )

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

    def _format_bubble_label(self, name: str, status: str) -> str:
        glyph = self._TOOL_STATUS_GLYPHS.get(status, "⋯")
        label = name or "tool"
        # Strip `mcp__<server>__` prefix for readability — the full
        # tool id is still visible in the expanded detail panel.
        if label.startswith("mcp__"):
            tail = label.split("__", 2)
            if len(tail) >= 3:
                label = tail[2]
        return f"{label} {glyph}"

    def _update_bubble_widget(self, slot: dict) -> None:
        """Rewrite the bubble button label + status CSS class from
        the slot's current name/status. Called by both append and
        update to keep one rendering path."""
        button: Gtk.Button = slot["button"]
        button.set_label(self._format_bubble_label(slot["name"], slot["status"]))
        for cls in ("pending", "running", "completed", "cancelled"):
            if cls == slot["status"]:
                button.add_css_class(cls)
            else:
                button.remove_css_class(cls)

    def _render_bubble_details(self, slot: dict) -> None:
        """Write the args preview + result text into the slot's
        detail label. Called lazily when the panel becomes visible
        (initial toggle) and on every subsequent status/result
        update so the panel stays in sync."""
        label: Gtk.Label = slot["details_label"]
        name = slot.get("name") or ""
        args = slot.get("arguments") or ""
        args_pretty = format_tool_args(name, args)
        parts = [f"<b>{GLib.markup_escape_text(name)}</b>"]
        if args_pretty:
            parts.append(f"<tt>{GLib.markup_escape_text(args_pretty)}</tt>")
        result = slot.get("result")
        if result:
            parts.append(f"<i>result:</i>\n<tt>{GLib.markup_escape_text(str(result))}</tt>")
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
        # event rather than another turn card.
        name_label = Gtk.Label(label=call.name or "(unnamed tool)", xalign=0.0)
        name_label.add_css_class("pilot-permission-tool-name")
        card.append(name_label)

        # Argument preview. Routes through `format_tool_args` so
        # common tool names (Bash / Read / Edit / …) render as a
        # short readable summary (`$ ls -la`, `📖 /path/to/file`)
        # instead of a JSON dump. Unknown tools still fall through to
        # the JSON-pretty branch inside the formatter.
        pretty = format_tool_args(call.name or "", call.arguments or "")
        args_label = Gtk.Label(label=pretty, xalign=0.0, hexpand=True)
        args_label.set_wrap(True)
        args_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        args_label.set_selectable(True)
        args_label.add_css_class("pilot-permission-args")
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

class PilotWindow(Gtk.ApplicationWindow):
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
    ):
        super().__init__(application=app, title="Pilot")
        # `.overlay` is our root scope — every CSS rule is namespaced under
        # it so theme selectors like `window { background: … }` can't win.
        self.add_css_class("overlay")
        self._adapter = adapter
        # Session handle wired later via `attach_session` — the overlay
        # uses it to push auto-list mutations into the authoritative
        # Session state (which the MCP subprocess polls over the bridge
        # socket). None-safe so tests can construct windows without a
        # live socket.
        self._session: Optional["Session"] = None
        # HTTP adapter carries a model; claude/codex wrap CLIs that pick
        # their own. Model string can be empty → treat as absent.
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
        # Tool permission rows live above the queue; trust set is
        # per-session. The trust set is ALSO the pre-flight gate: the
        # adapter's `extra_body["tool_ids"]` is rewritten from this set
        # before every turn, so untrusted tools never reach the server.
        # Rows in the panel are a live audit trail of tools the server
        # DID run (trusted) plus anything surprising that slipped
        # through. Seed from whatever the adapter was constructed with
        # so CLI defaults (`--extra-body '{"tool_ids": [...]}'`)
        # pre-populate trust.
        self._permissions: list[PermissionRow] = []
        extra_body = getattr(adapter, "extra_body", None) or {}
        initial_tool_ids = extra_body.get("tool_ids") if isinstance(extra_body, dict) else None
        self._trusted_tools: set[str] = set(initial_tool_ids or [])
        # Auto-lists are a SEPARATE short-circuit from trust:
        # - `_auto_approved_tools` — responses come back `allow` without
        #   the user seeing a row. Rendered as green pills.
        # - `_auto_rejected_tools` — responses come back `deny` without
        #   the user seeing a row. Rendered as red pills.
        # These sets drive UI rendering; the Session owns the
        # authoritative copy the MCP subprocess consults over the
        # bridge. Seeded from CLI args in parallel with the Session so
        # both start identical on session launch.
        self._auto_approved_tools: set[str] = set(auto_approve or [])
        self._auto_rejected_tools: set[str] = set(auto_reject or [])
        self._install_css()

        Gtk4LayerShell.init_for_window(self)
        # Explicit namespace so compositor layerrules can target us
        # (`layerrule = blur, pilot`) without regex-escaping the app-id.
        Gtk4LayerShell.set_namespace(self, "pilot")
        Gtk4LayerShell.set_layer(self, Gtk4LayerShell.Layer.TOP)
        Gtk4LayerShell.set_anchor(self, Gtk4LayerShell.Edge.TOP, True)
        Gtk4LayerShell.set_anchor(self, Gtk4LayerShell.Edge.BOTTOM, True)
        Gtk4LayerShell.set_anchor(self, Gtk4LayerShell.Edge.RIGHT, True)
        # EXCLUSIVE so toggle-window / present() grab keyboard focus the
        # moment we map. Escape hides → grab releases automatically.
        Gtk4LayerShell.set_keyboard_mode(self, Gtk4LayerShell.KeyboardMode.EXCLUSIVE)
        self.set_default_size(self._overlay_width(), -1)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.add_css_class("pilot-root")

        # Header --------------------------------------------------------
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        header.add_css_class("pilot-header")
        self._provider_label = Gtk.Label(
            label=self._header_title(),
            xalign=0.0,
            hexpand=True,
        )
        self._provider_label.add_css_class("pilot-provider")
        self._provider_label.add_css_class("idle")
        close_btn = Gtk.Button(label="✕")
        close_btn.add_css_class("pilot-close")
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

        self.set_child(root)

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
        self._stream_started = False
        self._turn_cancelled = False
        # Compose stays enabled: the queue system handles anything the
        # user types while this turn is streaming — new submissions get
        # pushed behind the current stream automatically.
        self._update_phase()
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

    def _append_thinking(self, chunk: str) -> bool:
        """Main-thread-safe sink for `ThinkingChunk` events. Routes
        the reasoning text into the active assistant card's
        collapsible thinking section. Returns False so it composes
        with `GLib.idle_add`."""
        if self._active_assistant is not None:
            self._active_assistant.append_thinking(chunk)

        return False

    def _run_turn(self, user_message: str) -> None:
        first_text_chunk = True
        try:
            for chunk in self._adapter.turn(user_message):
                if not self._alive:
                    return
                # Tool events surface in the permission panel as an
                # audit trail; they do NOT pause the stream. Real
                # gating happens pre-flight via `_trusted_tools` ->
                # `adapter.extra_body["tool_ids"]` — an untrusted tool
                # simply isn't advertised to the server, so the AI
                # can't call it.
                # Mid-stream blocking would just desync us from the
                # server's native-function-calling protocol (server
                # ends the stream at `finish_reason: tool_calls`
                # expecting us to send tool results; blocking reads
                # doesn't help because by the time we see the event
                # the turn is already done).
                if isinstance(chunk, ToolCall):
                    # `audit=True` is the new normal — bubble strip on
                    # the active assistant card. `audit=False` goes to
                    # the legacy permission-row sink (kept for MCP
                    # bridge-initiated paths and any future non-audit
                    # producers).
                    if getattr(chunk, "audit", False):
                        GLib.idle_add(self._on_tool_stream_event, chunk)
                    else:
                        GLib.idle_add(self._on_tool_call, chunk)
                    continue
                if isinstance(chunk, ThinkingChunk):
                    GLib.idle_add(self._append_thinking, chunk.text)
                    continue
                if first_text_chunk:
                    GLib.idle_add(self._mark_stream_started)
                    first_text_chunk = False
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
    _NOTIFY_ICON_QUESTION = "dialog-question-symbolic"

    def _should_notify(self) -> bool:
        """Only fire desktop toasts when the overlay itself is hidden.
        If it's on screen the provider pill + the new row / banner
        already tell the user what happened — doubling up with a
        notification is just noise. Layer-shell surfaces don't have a
        meaningful `is_active()` (the compositor keeps them focusable
        on-demand only), so visibility alone is the right gate."""
        return not self.get_visible()

    def _notify_finished(self) -> None:
        if not self._should_notify():
            return
        notify("Pilot", "Response finished", self._NOTIFY_ICON_FINISHED, timeout=3000)

    def _notify_approval(self, tool_name: str) -> None:
        if not self._should_notify():
            return
        label = tool_name or "tool"
        notify(
            "Pilot — approval needed",
            f"Waiting on approval: {label}",
            self._NOTIFY_ICON_APPROVAL,
            timeout=8000,
        )

    def _notify_question(self, question: str) -> None:
        if not self._should_notify():
            return
        preview = question if len(question) <= 140 else question[:137] + "…"
        notify(
            "Pilot — question",
            preview or "The AI is waiting for your answer",
            self._NOTIFY_ICON_QUESTION,
            timeout=10000,
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
        if self._permissions or self._compose.has_question():
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

        return False

    def _on_tool_call(self, call: ToolCall) -> bool:
        """Main-thread sink for `ToolCall` events from `adapter.turn()`.
        Scheduled via `GLib.idle_add` from the worker thread — returns
        False so it fires once and detaches."""
        if call.name and call.name in self._trusted_tools:
            log.debug("tool %r is trusted; skipping permission row", call.name)

            return False
        # Belt-and-suspenders: the MCP subprocess's approval route
        # should have short-circuited before any row reaches this
        # branch, but non-Claude adapters (HTTP, codex) bypass MCP
        # entirely and still surface ToolCall events through the audit
        # trail. Filter those here so the pills and the audit trail
        # stay consistent.
        if call.name:
            if call.name in self._auto_approved_tools:
                log.debug("tool %r auto-approved; skipping row", call.name)
                return False
            if call.name in self._auto_rejected_tools:
                log.debug("tool %r auto-rejected; skipping row", call.name)
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

    def show_question(self, question: str, resolve) -> bool:
        """Route a claude `ask_question` MCP tool through the overlay.

        Places a question banner inside the compose wrap so the
        question reads right above the input box; the user's next
        submit sends the typed text back to `resolve` instead of
        dispatching a normal turn. The banner disappears as soon as
        they answer (or skip). Returns False so `GLib.idle_add` fires
        once."""

        # Wrap `resolve` so the phase pill flips back to whatever it
        # was before the question arrived (pending / streaming /
        # idle) as soon as the user answers or skips. The compose
        # clears `_question_callback` before firing the callback, so
        # by the time we reach `_update_phase()` `has_question()`
        # already reports False.
        def resolved(answer: str) -> None:
            try:
                resolve(answer)
            finally:
                self._update_phase()

        self._compose.set_question_mode(question, on_answer=resolved)
        # Always focus the compose textview when a question lands,
        # even if focus is currently on an approval row or elsewhere.
        # The `idle_add` defer ensures the grab fires after the
        # banner's size-allocate, otherwise grab_focus on a freshly-
        # laid-out widget can no-op.
        self._compose.focus()
        GLib.idle_add(self._compose.focus)
        self._update_phase()
        self._notify_question(question)

        return False

    def show_permission_for_mcp(self, call: ToolCall, resolve) -> bool:
        """Render a permission row for a Claude MCP permission-prompt
        request and invoke `resolve(approved: bool, reason: str)` when
        the user clicks. This is the REAL gate for `--converse-provider
        claude` — the pilot MCP subprocess is blocked on a socket round-
        trip here, so claude's subprocess won't run the tool until we
        return. Returns False so `GLib.idle_add` fires once.

        Belt-and-suspenders: if the tool is in an auto list the MCP
        subprocess's socket_auto_check route should have resolved the
        request upstream. If a request still reaches us (racy mutation,
        first-call-per-tool, etc.) we resolve without surfacing a row
        so the UI stays consistent with the pill state."""
        name = call.name or ""
        if name and name in self._auto_rejected_tools:
            resolve(False, f"auto-rejected: {name}")
            return False
        if name and name in self._auto_approved_tools:
            resolve(True, "")
            return False

        def on_allow(r: PermissionRow) -> None:
            self._remove_permission_row(r)
            resolve(True, "")

        def on_trust(r: PermissionRow) -> None:
            name = r.tool_name
            if name:
                self._trusted_tools.add(name)
                self._sync_permission_state()
            for existing in list(self._permissions):
                if existing.tool_name == name:
                    self._remove_permission_row(existing)
            resolve(True, "")

        def on_deny(r: PermissionRow) -> None:
            name = r.tool_name or "tool"
            self._remove_permission_row(r)
            resolve(False, f"user denied {name}")

        def on_auto_reject(r: PermissionRow) -> None:
            # Add to auto-reject, push to session so the MCP subprocess
            # sees the update for subsequent calls, cancel the turn,
            # stamp the assistant card, clear the row + any siblings
            # for the same tool. The `resolve(False)` path finalises
            # the current in-flight MCP request — subsequent requests
            # for the same tool will short-circuit upstream via the
            # socket_auto_check route.
            name = r.tool_name or "tool"
            if r.tool_name:
                self._auto_rejected_tools.add(r.tool_name)
                # Removing from auto_approve keeps mutual exclusion
                # honest — a tool in both sets would pick reject
                # (reject wins in McpServer's normalisation), but UI
                # wise we shouldn't show both pills.
                self._auto_approved_tools.discard(r.tool_name)
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
                if existing.tool_name == r.tool_name:
                    self._remove_permission_row(existing)
            resolve(False, f"auto-rejected: {name}")

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
            self._trusted_tools.add(name)
            self._sync_permission_state()
        # Drop every pending row for the same tool while we're at it —
        # the user just said they trust it, no point keeping duplicate
        # prompts for concurrent calls on screen.
        for existing in list(self._permissions):
            if existing.tool_name == name:
                self._remove_permission_row(existing)

    def _on_permission_auto_reject(self, row: PermissionRow) -> None:
        """Non-MCP auto-reject path. Same shape as the MCP variant in
        `show_permission_for_mcp` but without the resolver — this
        fires for ToolCall audit rows, which are fire-and-forget."""
        name = row.tool_name or "tool"
        if row.tool_name:
            self._auto_rejected_tools.add(row.tool_name)
            self._auto_approved_tools.discard(row.tool_name)
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

    def _untrust_tool(self, name: str) -> None:
        """Remove `name` from the per-session trust set so future turns
        stop advertising it to the server. Wired to the compose-bar
        pill click handler."""
        if name in self._trusted_tools:
            self._trusted_tools.discard(name)
            self._sync_permission_state()

    def attach_session(self, session: "Session") -> None:
        """Wire a live Session onto the window so permission actions
        (pill clicks, ⛔ auto-reject button) can push auto-list
        mutations into the authoritative Session state. The window
        stays functional without a session attached — mutations just
        stay local, which matches the expected behaviour for tests and
        non-MCP providers that don't spawn an approval bridge."""
        self._session = session
        # Seed the session's lists from the window so both sides
        # agree at attach time (the session has its own CLI-seed path;
        # this is belt-and-suspenders for construction orderings that
        # pass lists to the window but not the session).
        session.set_auto_lists(
            approve=sorted(self._auto_approved_tools),
            reject=sorted(self._auto_rejected_tools),
        )
        self._sync_permission_state()

    def _sync_tool_gate(self) -> None:
        """Kept for back-compat — older callers use this name. Routes
        through `_sync_permission_state` which handles the full (trust
        + auto-approve + auto-reject) tri-list sync."""
        self._sync_permission_state()

    def _sync_permission_state(self) -> None:
        """Push the current trust / auto-approve / auto-reject sets
        into three places so every consumer sees the same truth:

        1. `adapter.extra_body["tool_ids"]` — pre-flight trust gate
           for HTTP adapters. Unchanged from `_sync_tool_gate`'s
           original responsibility.
        2. `Session._auto_approve` / `_auto_reject` — authority the
           MCP subprocess consults over the bridge socket. Only
           touched when a Session is attached (tests skip).
        3. Compose-bar pills — visual reflection of all three sets
           with click-to-remove affordances per list.

        Called from attach_session and every time the user mutates
        trust or an auto-list via a pill click / permission button."""
        extra_body = getattr(self._adapter, "extra_body", None)
        if isinstance(extra_body, dict):
            if self._trusted_tools:
                extra_body["tool_ids"] = sorted(self._trusted_tools)
            else:
                # Leave no `tool_ids` key at all when nothing is trusted
                # — the server then sees the request with zero advertised
                # tools, matching the pre-refactor behaviour where
                # tool_ids=None suppressed the field entirely.
                extra_body.pop("tool_ids", None)
        if self._session is not None:
            self._session.set_auto_lists(
                approve=sorted(self._auto_approved_tools),
                reject=sorted(self._auto_rejected_tools),
            )
        self._compose.set_permission_pills(
            trusted=sorted(self._trusted_tools),
            auto_approved=sorted(self._auto_approved_tools),
            auto_rejected=sorted(self._auto_rejected_tools),
            on_remove=self._on_pill_remove,
        )

    def _on_pill_remove(self, name: str, kind: str) -> None:
        """Compose-bar pill click handler. `kind` picks which list the
        tool gets removed from; the caller (ComposeView) passes
        whichever list the clicked pill was rendered from, so each
        pill's click affordance stays predictable (click green pill →
        leaves auto-approve, click red → leaves auto-reject, etc.)."""
        match kind:
            case "trusted":
                self._untrust_tool(name)
            case "auto_approve":
                self._auto_approved_tools.discard(name)
                self._sync_permission_state()
            case "auto_reject":
                self._auto_rejected_tools.discard(name)
                self._sync_permission_state()

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
        if tool_name in self._trusted_tools:
            self._trusted_tools.discard(tool_name)
            self._sync_permission_state()
        for existing in list(self._permissions):
            self._remove_permission_row(existing)

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

    def _paste_clipboard_into_compose(self) -> None:
        text = InputAdapterClipboard().read() or ""
        if not text:
            return
        self._compose.focus()
        self._compose.append_text(text)

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
        focused, resize width to 40% of that output, and cap the compose
        scroller at 25% of that output's height. Called on every
        toggle-to-visible so monitor switches rehome the overlay.

        A layer-shell surface's size follows the widget's requested
        size on commit. `set_default_size` + `set_size_request` +
        `queue_resize` marks the widget tree dirty; the next map cycle
        (toggle hides + shows) then commits a new layer surface with
        the updated dimensions. We do NOT manually call `unmap()` /
        `map()` here — on some builds that emits the `map` signal
        twice, which makes GTK re-append the compose bar every toggle
        (duplicate send buttons stacking at the bottom)."""
        monitor = _focused_gdk_monitor()
        if monitor is not None:
            Gtk4LayerShell.set_monitor(self, monitor)
        width = self._overlay_width(monitor=monitor)
        self.set_default_size(width, -1)
        self.set_size_request(width, -1)
        self.queue_resize()
        if monitor is not None:
            self._compose.set_max_content_fraction(monitor.get_geometry().height, 0.25)

    @staticmethod
    def _install_css() -> None:
        """Load `pilot.css` from alongside this script and register it at
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
        css = load_relative_file("pilot.css", relative_to=__file__).encode("utf-8")
        provider = Gtk.CssProvider()

        def _on_error(_prov, section, err):
            start = section.get_start_location()
            log.warning(
                "pilot.css parse error at line %d col %d: %s",
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
    """Owns the Unix socket for a live pilot window. A background thread
    accepts connections from forwarder invocations and dispatches their
    `turn` / `status` commands back onto the GTK main thread.

    Also acts as the AUTHORITATIVE source for auto-approve /
    auto-reject sets. The MCP subprocess pings us over the bridge
    socket via `auto-check` before every approval request; the user
    mutates the sets by clicking pills / the ⛔ button in the overlay,
    and the overlay forwards the new state into this object via
    `set_auto_lists()`. Using a Lock around the sets keeps socket
    handler threads (serving `auto-check`) and the GTK thread (pushing
    mutations) from racing."""

    def __init__(
        self,
        window: PilotWindow,
        provider: ConversationProvider,
        auto_approve: Optional[list[str]] = None,
        auto_reject: Optional[list[str]] = None,
    ):
        self._window = window
        self._provider = provider
        self._sock: Optional[socket.socket] = None
        self._mcp_sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._mcp_thread: Optional[threading.Thread] = None
        self._auto_lock = threading.Lock()
        # Normalise on insert so the mcp-server subprocess's canonical
        # form (`lower + replace("-", "_")`) matches what it compares
        # against over the bridge. Keep the raw forms readable — the
        # overlay uses the un-normalised form for UI pills, but the
        # authority needs canonical for correctness.
        self._auto_approve: set[str] = set(
            self._normalise_name(n) for n in (auto_approve or [])
        )
        self._auto_reject: set[str] = set(
            self._normalise_name(n) for n in (auto_reject or [])
        )

    @staticmethod
    def _normalise_name(name: str) -> str:
        """Match `McpServer._normalise_tool_name` so the authority's
        canonical form lines up with the subprocess's lookup keys."""
        return (name or "").lower().replace("-", "_")

    def set_auto_lists(self, approve: list[str], reject: list[str]) -> None:
        """Replace the in-memory auto lists wholesale. Called from the
        overlay whenever the user mutates a pill / button; subsequent
        `auto-check` lookups from the MCP subprocess see the new
        state immediately."""
        with self._auto_lock:
            self._auto_approve = set(self._normalise_name(n) for n in approve)
            self._auto_reject = set(self._normalise_name(n) for n in reject)

    def _auto_decision(self, tool_name: str) -> str:
        """Return `"approve"`, `"reject"`, or `"continue"` for the
        given tool name. Reject wins over approve when both lists
        somehow contain the same tool — matches McpServer's local
        route ordering."""
        key = self._normalise_name(tool_name)
        with self._auto_lock:
            if key in self._auto_reject:
                return "reject"
            if key in self._auto_approve:
                return "approve"

            return "continue"

    def _bind(self, path: str, live_check: bool) -> socket.socket:
        """Bind a listener to `path`, cleaning up stale socket files.
        Set `live_check=True` only for the main socket — there we must
        detect another running session; for the MCP socket we can
        always clobber a stale file because it's only meaningful while
        OUR session is alive."""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(path)
        except OSError as e:
            if e.errno != errno.EADDRINUSE:
                sock.close()
                raise
            if live_check and _is_live():
                sock.close()
                raise RuntimeError(f"another pilot session is already running at {path}")
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            sock.bind(path)

        os.chmod(path, 0o600)
        sock.listen(4)

        return sock

    def start(self) -> None:
        self._sock = self._bind(SOCKET_PATH, live_check=True)
        self._mcp_sock = self._bind(MCP_SOCKET_PATH, live_check=False)
        self._thread = threading.Thread(
            target=self._serve, args=(self._sock,), daemon=True
        )
        self._thread.start()
        self._mcp_thread = threading.Thread(
            target=self._serve, args=(self._mcp_sock,), daemon=True
        )
        self._mcp_thread.start()

    def stop(self) -> None:
        for attr, path in (
            ("_sock", SOCKET_PATH),
            ("_mcp_sock", MCP_SOCKET_PATH),
        ):
            sock = getattr(self, attr, None)
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
                setattr(self, attr, None)
            try:
                os.unlink(path)
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
            case "permission":
                # Claude's MCP permission-prompt-tool forwards here.
                # Block this socket handler thread until the user
                # clicks allow/deny in the overlay, then return the
                # answer. The MCP server translates the `approved`
                # flag into claude's allow/deny envelope.
                tool_name = (obj.get("tool_name") or "").strip()
                arguments = obj.get("arguments") or ""
                event = threading.Event()
                verdict = {"approved": False, "reason": "timeout"}

                def resolve(approved: bool, reason: str = "") -> None:
                    verdict["approved"] = bool(approved)
                    verdict["reason"] = reason
                    event.set()

                call = ToolCall(
                    tool_id=f"mcp-{id(event)}",
                    name=tool_name,
                    arguments=str(arguments),
                    status="running",
                    audit=False,
                )
                GLib.idle_add(self._window.show_permission_for_mcp, call, resolve)
                # 10-min ceiling matches `socket_approval`'s timeout.
                if not event.wait(timeout=600):
                    verdict = {"approved": False, "reason": "overlay timeout"}

                return verdict
            case "auto-check":
                # MCP subprocess asks whether to short-circuit this
                # tool call without surfacing a row. Pure in-memory
                # lookup — no GTK hop, no user interaction, answers
                # instantly. `continue` tells the subprocess to fall
                # through to its next route (local fallback, question
                # route, normal approval flow).
                tool_name = (obj.get("tool_name") or "").strip()
                decision = self._auto_decision(tool_name)
                response: dict[str, Any] = {"decision": decision}
                if decision == "reject":
                    response["message"] = f"auto-rejected: {tool_name}"

                return response
            case "question":
                # Claude's `ask_question` MCP tool forwards here. Block
                # the handler thread until the user types an answer
                # into compose and hits send; return the answer string.
                question = (obj.get("question") or "").strip()
                q_event = threading.Event()
                q_holder = {"answer": ""}

                def q_resolve(answer: str) -> None:
                    q_holder["answer"] = answer or ""
                    q_event.set()

                GLib.idle_add(self._window.show_question, question, q_resolve)
                if not q_event.wait(timeout=600):
                    return {"answer": ""}

                return {"answer": q_holder["answer"]}
            case _:
                return {"ok": False, "error": f"unhandled command: {cmd!r}"}

def _build_mcp_config(
    auto_approve: Optional[list[str]] = None,
    auto_reject: Optional[list[str]] = None,
    agents_md: Optional[str] = None,
    skills_dir: Optional[str] = None,
    default_mcp: Optional[list[str]] = None,
) -> McpConfig:
    """Compose the `McpConfig` we hand to the Claude adapter.

    Pre-seeds one entry — the `pilot` server, which re-execs THIS SAME
    script with `mcp-server` as argv[1]. The top of pilot.py short-
    circuits to `lib.mcp.McpServer.run()` for that argv and exits
    without touching GTK, so the subprocess is a pure stdio server.
    Callers can layer additional servers (github, filesystem, …) by
    mutating the returned `McpConfig` before it reaches the adapter.

    When SESSION_SUFFIX is set we propagate `--session <suffix>` onto
    the child's argv so its early `_early_session_suffix()` parse
    picks the matching MCP socket path — otherwise the child would
    try to reach the default socket while our overlay is listening on
    the suffixed one.

    `auto_approve` / `auto_reject` lists get splatted onto the child
    as repeated `--auto-approve X` / `--auto-reject Y` flags so the
    subprocess's local auto-list sets are seeded at startup (matches
    the overlay's Session state for this invocation). Runtime
    mutations flow over the MCP bridge socket via the `auto-check`
    command, so the child stays in sync without being restarted.

    `agents_md` / `skills_dir` are likewise splatted so the
    subprocess's early parser can wire up `McpCapability.RESOURCE`
    (for AGENTS.md) and `McpCapability.SKILLS` (for the list/load
    tools). Both are optional — skip either to leave that capability
    out of the child's registered tool list.

    `default_mcp` names are resolved against `lib.default_servers` and
    mixed into the final config as peer servers alongside `pilot`. We
    expand env-vars at this point so the spawned stdio binaries /
    remote endpoints see the user's actual credentials; missing vars
    collapse to empty string (the catalog's logging handles the
    warning)."""
    self_script = os.path.abspath(__file__)
    child_args = ["-u", self_script, "mcp-server"]
    if SESSION_SUFFIX:
        child_args.extend(["--session", SESSION_SUFFIX])
    for name in auto_approve or []:
        child_args.extend(["--auto-approve", name])
    for name in auto_reject or []:
        child_args.extend(["--auto-reject", name])
    if agents_md:
        child_args.extend(["--agents-md", agents_md])
    if skills_dir:
        child_args.extend(["--skills-dir", skills_dir])
    # `--default-mcp` is splatted too so the subprocess COULD act on it
    # in future, even though today it's only the parent claude adapter
    # that consumes the expanded server specs. Keeps the flag surface
    # symmetrical between the two halves.
    for name in default_mcp or []:
        child_args.extend(["--default-mcp", name])
    config = McpConfig()
    config.add("pilot", sys.executable, args=child_args)
    # Layer in any requested default servers. Unknown names log a
    # warning and skip — we don't want a typo on --default-mcp to
    # prevent the overlay from opening.
    for raw_name in default_mcp or []:
        name = (raw_name or "").strip()
        if not name:
            continue
        try:
            spec = _DEFAULT_SERVER_GET(name)
        except KeyError as e:
            log.warning("ignoring unknown --default-mcp %r: %s", name, e)
            continue
        config.add(
            name,
            command=spec.get("command"),
            args=spec.get("args"),
            env=spec.get("env"),
            url=spec.get("url"),
            type=spec.get("type"),
            headers=spec.get("headers"),
        )

    return config

def _merge_extra_body(blobs) -> dict:
    """Deep-merge an ordered list of `--extra-body` JSON strings (or
    pre-parsed dicts) into a single dict. Later entries win on leaf
    keys; nested dicts merge recursively via `_deep_merge`. Empty
    list → empty dict."""
    out: dict = {}
    for blob in blobs or []:
        if isinstance(blob, dict):
            parsed = blob
        else:
            try:
                parsed = json.loads(blob)
            except json.JSONDecodeError as e:
                raise ValueError(f"--extra-body: not valid JSON: {blob!r}: {e}") from e
        if not isinstance(parsed, dict):
            raise ValueError(
                f"--extra-body: expected a JSON object, got {type(parsed).__name__}"
            )
        out = _deep_merge(out, parsed)

    return out

def _resolve_default_mcp(args) -> list[str]:
    """Collapse `--default-mcp` / `--all-default-mcp` into a de-duped
    ordered list. `--all-default-mcp` wins when set — every catalog
    entry is added in display order; individual `--default-mcp` names
    are appended after (skipping dupes). Unknown names survive to
    `_build_mcp_config` which logs and skips them; we don't validate
    here so CLI typos don't crash toggle startup."""
    out: list[str] = []
    seen: set[str] = set()

    def _push(name: str) -> None:
        n = (name or "").strip()
        if not n or n in seen:
            return
        seen.add(n)
        out.append(n)

    if getattr(args, "all_default_mcp", False):
        for name in DEFAULT_SERVER_NAMES:
            _push(name)
    for name in getattr(args, "default_mcp", None) or []:
        _push(name)

    return out


def _read_agents_md(path: Optional[str]) -> str:
    """Return the AGENTS.md contents (or empty string if missing / not
    configured). Any read error degrades to "" + a warning — we never
    block `toggle` on a missing bootstrap file."""
    if not path:
        return ""
    expanded = os.path.expanduser(path)
    if not os.path.isfile(expanded):
        log.warning("--agents-md %s: file not found; skipping injection", expanded)
        return ""
    try:
        with open(expanded, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError as e:
        log.warning("--agents-md %s: read failed (%s); skipping injection", expanded, e)
        return ""


def _compose_system_prompt(base: str, agents_md: str) -> str:
    """Prepend `agents_md` to `base`, separated by a blank line, iff
    `agents_md` has content. Otherwise return `base` unchanged so we
    don't introduce a leading newline into the default prompt."""
    if not agents_md:
        return base

    return f"{agents_md}\n\n{base}"


def _build_adapter(args) -> ConversationAdapter:
    # Callers pass argparse values through as-is. None / missing values
    # collapse to per-adapter defaults (see `kwargs.get(name) or DEFAULT`
    # in the adapter __init__s), so this function doesn't replicate them.
    provider = ConversationProvider(args.converse_provider)
    extra_body = _merge_extra_body(getattr(args, "extra_body", None))
    cwd = getattr(args, "cwd", None)
    config_suffix = SESSION_SUFFIX or None
    auto_approve = list(getattr(args, "auto_approve", None) or [])
    auto_reject = list(getattr(args, "auto_reject", None) or [])
    agents_md_path = getattr(args, "agents_md", None)
    skills_dir = getattr(args, "skills_dir", None)
    default_mcp = _resolve_default_mcp(args)
    system_prompt = _compose_system_prompt(
        AI_SYSTEM_PROMPT,
        _read_agents_md(agents_md_path),
    )
    match provider:
        case ConversationProvider.HTTP:
            return ConversationAdapterHttp(
                system_prompt,
                base_url=args.converse_base_url,
                model=args.converse_model,
                api_key=os.environ.get("AI_KILIC_DEV_API_KEY", ""),
                temperature=args.converse_temperature,
                top_p=args.converse_top_p,
                thinking=args.converse_thinking,
                num_ctx=args.converse_num_ctx,
                extra_body=extra_body,
                cwd=cwd,
                user_agent="pilot/1.0",
            )
        case ConversationProvider.CLAUDE:
            return ConversationAdapterClaude(
                system_prompt,
                model=args.converse_model,
                mcp_config=_build_mcp_config(
                    auto_approve=auto_approve,
                    auto_reject=auto_reject,
                    agents_md=agents_md_path,
                    skills_dir=skills_dir,
                    default_mcp=default_mcp,
                ),
                permission_tool="mcp__pilot__approve",
                cwd=cwd,
                config_suffix=config_suffix,
            )
        case ConversationProvider.CODEX:
            return ConversationAdapterCodex(
                system_prompt,
                model=args.converse_model,
                cwd=cwd,
            )
        case ConversationProvider.OPENCODE:
            return ConversationAdapterOpenCode(
                system_prompt,
                model=args.converse_model,
                # Same MCP config the claude branch uses — opencode
                # supports MCP servers natively, we just splice ours
                # into its config JSON.
                mcp_config=_build_mcp_config(
                    auto_approve=auto_approve,
                    auto_reject=auto_reject,
                    agents_md=agents_md_path,
                    skills_dir=skills_dir,
                    default_mcp=default_mcp,
                ),
                cwd=cwd,
                config_suffix=config_suffix,
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

    app = Gtk.Application(application_id=APP_ID)
    session: dict[str, Optional[Session]] = {"server": None}

    auto_approve = list(getattr(args, "auto_approve", None) or [])
    auto_reject = list(getattr(args, "auto_reject", None) or [])

    def on_activate(application):
        window = PilotWindow(
            application,
            adapter,
            auto_approve=auto_approve,
            auto_reject=auto_reject,
        )
        server = Session(
            window,
            adapter.provider,
            auto_approve=auto_approve,
            auto_reject=auto_reject,
        )
        # The window needs a handle on the Session so permission-button
        # handlers (e.g. the new ⛔ auto-reject button) can push list
        # mutations straight into the authoritative Session state.
        window.attach_session(server)
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
    icon = "󱍊"
    badge = f"<sup>{queue}</sup>" if queue > 0 else ""
    text = f"{icon}{badge}"
    match phase:
        case "streaming":
            tooltip = f"Pilot: streaming via {provider}"
        case "pending":
            tooltip = f"Pilot: waiting on first chunk from {provider}"
        case "awaiting":
            tooltip = f"Pilot: waiting on user input ({provider})"
        case _:
            tooltip = f"Pilot: {provider} idle"
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
            os.unlink(SOCKET_PATH)
        except FileNotFoundError:
            pass
    _signal_waybar_safe()

_DEFAULT_EXTRA_BODY = '{"tool_ids": ["web_search", "memory"]}'

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
    # Working directory for the spawned AI agent (claude / codex /
    # opencode subprocess). Default = a fresh `mkdtemp` so each
    # session runs in a clean-room sandbox the user hasn't had to
    # prepare. Override with `--cwd ~/notes` to point the agent at a
    # real project.
    toggle_parser.add_argument(
        "--cwd",
        default=None,
        metavar="PATH",
        help=(
            "working directory for the spawned AI CLI. Defaults to a "
            "fresh tempdir (tempfile.mkdtemp(prefix='pilot-'))."
        ),
    )
    # Generic HTTP-body escape hatch. Replaces the older `--tool-id`
    # flag: anything you want layered onto the OpenWebUI / OpenAI-
    # compatible request body goes here as JSON. Multiple flags
    # deep-merge (later wins on leaf keys, nested dicts merge).
    # The default bundles the old `tool_ids=["web_search","memory"]`
    # behaviour so existing users see zero change.
    toggle_parser.add_argument(
        "--extra-body",
        action="append",
        dest="extra_body",
        default=[_DEFAULT_EXTRA_BODY],
        metavar="JSON",
        help=(
            "JSON object deep-merged into the HTTP request body. "
            "Repeatable; later values win on leaf keys, nested dicts "
            "merge recursively. Default: "
            f"{_DEFAULT_EXTRA_BODY}. OpenWebUI / OpenAI-compatible only."
        ),
    )
    # Auto-approve: tool names whose MCP permission requests are
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
            "Default: ~/.config/nvim/utils/agents/skills"
        ),
    )
    # Default MCP catalog. `--default-mcp NAME` is repeatable and
    # references an entry in `lib.default_servers.DEFAULT_SERVERS`.
    # Unknown names log + skip; missing env vars expand to "" rather
    # than crashing. Leave empty (the default) to opt out.
    toggle_parser.add_argument(
        "--default-mcp",
        action="append",
        dest="default_mcp",
        default=[],
        metavar="NAME",
        choices=list(DEFAULT_SERVER_NAMES) + [""],
        help=(
            "Name of a default MCP server to register alongside "
            "`pilot`. Repeatable. Valid names: "
            + ", ".join(DEFAULT_SERVER_NAMES)
        ),
    )
    # Shortcut for "every catalog entry". Mutually compatible with
    # `--default-mcp` — explicit names after the flag are de-duped.
    toggle_parser.add_argument(
        "--all-default-mcp",
        dest="all_default_mcp",
        action="store_true",
        default=False,
        help=(
            "Register every server in lib.default_servers in display "
            "order. Equivalent to passing each name via --default-mcp."
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

    _apply_session_suffix(args.session or "")

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

if __name__ == "__main__":
    main()
