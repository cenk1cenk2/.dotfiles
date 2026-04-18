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
import sys
import threading
from typing import Optional

from markdown_it import MarkdownIt

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Gtk4LayerShell, Pango  # noqa: E402

from lib import (
    DEFAULT_MODEL,
    ConversationAdapter,
    ConversationAdapterClaude,
    ConversationAdapterCodex,
    ConversationAdapterHttp,
    EnrichProvider,
    InputAdapterClipboard,
    InputAdapterStdin,
    InputMode,
    load_prompt,
)

log = logging.getLogger("ask")

APP_ID = "dev.kilic.wayland.ask"
SOCKET_PATH = os.path.join(
    os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}",
    "wayland-ask.sock",
)

AI_SYSTEM_PROMPT = load_prompt("ask.md", relative_to=__file__)

BASE_CSS = b"""
window {
    background: rgba(20, 22, 28, 0.96);
}
box.ask-root {
    border-radius: 12px 0 0 12px;
    box-shadow: -4px 0 12px rgba(0, 0, 0, 0.4);
}
box.ask-header {
    padding: 6px 10px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.08);
}
label.ask-provider {
    font-weight: bold;
    color: #d8dee9;
}
button.ask-close {
    background: transparent;
    border: none;
    padding: 2px 8px;
    color: #d8dee9;
}
button.ask-close:hover {
    color: #ffffff;
}
textview {
    font-size: 14pt;
    background: transparent;
}
textview text {
    background: transparent;
    color: #e5e9f0;
}
entry.ask-compose {
    font-family: monospace;
    padding: 8px 10px;
    margin: 8px;
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

class AskWindow(Gtk.ApplicationWindow):
    """Layer-shell sidebar anchored to the right edge, full-height.

    Public API: `dispatch_turn(user_message)` — append a user turn and
    stream the adapter's response into the markdown view. Safe to call
    from the GTK thread; the adapter runs in a worker."""

    def __init__(self, app: Gtk.Application, adapter: ConversationAdapter):
        super().__init__(application=app, title="Ask")
        self._adapter = adapter
        self._text = ""
        self._streaming = False
        self._alive = True
        self._install_css()

        Gtk4LayerShell.init_for_window(self)
        Gtk4LayerShell.set_layer(self, Gtk4LayerShell.Layer.TOP)
        Gtk4LayerShell.set_anchor(self, Gtk4LayerShell.Edge.TOP, True)
        Gtk4LayerShell.set_anchor(self, Gtk4LayerShell.Edge.BOTTOM, True)
        Gtk4LayerShell.set_anchor(self, Gtk4LayerShell.Edge.RIGHT, True)
        Gtk4LayerShell.set_keyboard_mode(self, Gtk4LayerShell.KeyboardMode.ON_DEMAND)
        self.set_default_size(420, -1)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.add_css_class("ask-root")

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        header.add_css_class("ask-header")
        provider_label = Gtk.Label(
            label=f"󰧑 {adapter.provider.value}",
            xalign=0.0,
            hexpand=True,
        )
        provider_label.add_css_class("ask-provider")
        close_button = Gtk.Button(label="✕")
        close_button.add_css_class("ask-close")
        close_button.connect("clicked", lambda _b: self.close())
        header.append(provider_label)
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

        self._compose = Gtk.Entry(hexpand=True)
        self._compose.add_css_class("ask-compose")
        self._compose.set_placeholder_text("paste / type …")
        self._compose.connect("activate", self._on_compose_submit)
        root.append(self._compose)

        self.set_child(root)

        self._md = MarkdownView(self._textview.get_buffer())

        self._wire_link_clicks()
        self._wire_keys()
        self.connect("close-request", self._on_close_request)

    def append(self, chunk: str) -> None:
        pin_to_bottom = self._at_bottom()
        self._text += chunk
        self._md.render(self._text)
        if pin_to_bottom:
            GLib.idle_add(self._scroll_to_end)

    def focus_compose(self) -> None:
        self._compose.grab_focus()

    def dispatch_turn(self, user_message: str) -> None:
        message = user_message.strip()
        if not message:
            return
        if self._streaming:
            log.info("ignoring turn while streaming")

            return
        self._append_user_turn(message)
        self._streaming = True
        self._compose.set_sensitive(False)
        threading.Thread(target=self._run_turn, args=(message,), daemon=True).start()

    def _append_user_turn(self, user_message: str) -> None:
        prefix = "\n\n---\n\n" if self._text else ""
        block = f"{prefix}### You:\n\n{user_message}\n\n### Assistant:\n\n"
        self.append(block)

    def _run_turn(self, user_message: str) -> None:
        try:
            for chunk in self._adapter.turn(user_message):
                if not self._alive:
                    return
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
            self._compose.grab_focus()

        return False

    def is_streaming(self) -> bool:
        return self._streaming

    def _on_compose_submit(self, entry: Gtk.Entry) -> None:
        text = entry.get_text()
        entry.set_text("")
        self.dispatch_turn(text)

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

    def _on_click(self, gesture, n_press, x, y) -> None:
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
        provider.load_from_data(BASE_CSS, len(BASE_CSS))
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

    def _on_key(self, controller, keyval, keycode, state) -> bool:
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        if ctrl and keyval == Gdk.KEY_q:
            self.close()
            return True
        if keyval == Gdk.KEY_Escape:
            self.close()
            return True

        return False

    def _on_close_request(self, _window) -> bool:
        self._alive = False
        try:
            self._adapter.close()
        except Exception as e:
            log.warning("adapter close failed: %s", e)

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

def _send(cmd: str, **extra) -> Optional[dict]:
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
        payload = json.dumps({"cmd": cmd, **extra}) + "\n"
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

    def __init__(self, window: AskWindow, provider: EnrichProvider):
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
                phase = "streaming" if self._window.is_streaming() else "idle"

                return {
                    "ok": True,
                    "phase": phase,
                    "provider": self._provider.value,
                }
            case _:
                return {"ok": False, "error": f"unhandled command: {cmd!r}"}

def _build_adapter(args) -> ConversationAdapter:
    provider = EnrichProvider(args.enrich_provider)
    match provider:
        case EnrichProvider.HTTP:
            features = (
                {k: True for k in args.features} if args.features else None
            )

            return ConversationAdapterHttp(
                system_prompt=AI_SYSTEM_PROMPT,
                base_url=args.enrich_base_url,
                model=args.enrich_model,
                api_key=os.environ.get("AI_KILIC_DEV_API_KEY", ""),
                temperature=args.enrich_temperature,
                top_p=args.enrich_top_p,
                thinking=args.enrich_thinking,
                num_ctx=args.enrich_num_ctx,
                tool_ids=args.tool_ids or None,
                features=features,
                user_agent="ask/1.0",
            )
        case EnrichProvider.CLAUDE:
            return ConversationAdapterClaude(AI_SYSTEM_PROMPT)
        case EnrichProvider.CODEX:
            return ConversationAdapterCodex(AI_SYSTEM_PROMPT)
        case _:
            raise ValueError(f"unknown enrich provider: {provider!r}")

def _read_input(mode: InputMode) -> str:
    match mode:
        case InputMode.STDIN:
            text = InputAdapterStdin().read()
        case InputMode.CLIPBOARD:
            text = InputAdapterClipboard().read()
        case _:
            raise ValueError(f"unknown input mode: {mode!r}")

    return (text or "").strip()

def main():
    parser = argparse.ArgumentParser(description="Conversational AI sidebar overlay")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument(
        "--input",
        type=InputMode,
        choices=[InputMode.STDIN, InputMode.CLIPBOARD],
        default=InputMode.STDIN,
        help="Source of the initial user turn",
    )
    parser.add_argument(
        "--enrich-provider",
        choices=["http", "claude", "codex"],
        default="claude",
    )
    parser.add_argument(
        "--enrich-base-url",
        default="https://ai.kilic.dev/api/v1",
    )
    parser.add_argument("--enrich-model", default="")
    parser.add_argument("--enrich-temperature", type=float)
    parser.add_argument("--enrich-top-p", type=float)
    parser.add_argument(
        "--enrich-thinking",
        nargs="?",
        const="high",
        default="none",
        choices=["high", "medium", "low", "none"],
    )
    parser.add_argument("--enrich-num-ctx", type=int)
    # OpenWebUI extensions for --enrich-provider=http. Ignored by other
    # providers and by plain OpenAI endpoints.
    parser.add_argument(
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
    parser.add_argument(
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
    args = parser.parse_args()

    logging.basicConfig(
        format="%(name)s: %(message)s",
        level=logging.DEBUG if args.verbose else logging.WARNING,
    )

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

            return False

        window.connect("close-request", on_close)
        window.present()
        window.focus_compose()
        if initial:
            window.dispatch_turn(initial)

    app.connect("activate", on_activate)
    try:
        app.run([sys.argv[0]])
    finally:
        server = session.get("server")
        if server:
            server.stop()

if __name__ == "__main__":
    main()
