#!/usr/bin/env python3

import argparse
import json
import logging
import os
import socket
import subprocess
import sys
import threading
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Optional, Protocol

from common import (
    ClaudeEnrichAdapter,
    ClipboardOutputAdapter,
    CodexEnrichAdapter,
    EnrichAdapter,
    EnrichProvider,
    HttpEnrichAdapter,
    OutputAdapter,
    OutputMode,
    TypeOutputAdapter,
    load_prompt,
    notify,
    signal_waybar,
)

DEFAULT_MODEL = "gemma4:31b-cloud"

class STTAdapter(Protocol):
    """Contract for a speech-to-text daemon driven by speech.py.

    Swap implementations here to target a different backend; the rest of
    speech.py talks only through this interface."""

    def is_recording(self) -> bool:
        """True while the backend is capturing audio."""
        ...

    def stop(self) -> None:
        """Finalise the current recording. Any transcription will be
        delivered through the subprocess returned by `capture()`."""
        ...

    def cancel(self) -> None:
        """Abort the current recording and discard the audio."""
        ...

    def capture(self) -> "subprocess.Popen[bytes]":
        """Subscribe to the backend's transcription stream.

        Subscribing must trigger recording if the backend is idle, or
        attach to an in-flight recording otherwise. The returned process
        must close stdout after delivering the final transcription so the
        caller can drive it with `communicate()`."""
        ...

class HyprwhsprAdapter:
    """Talks to the hyprwhspr daemon through its `record` CLI subcommands."""

    def is_recording(self) -> bool:
        result = subprocess.run(
            ["hyprwhspr", "record", "status"],
            capture_output=True,
            text=True,
            check=False,
        )
        return "Recording in progress" in (result.stdout + result.stderr)

    def stop(self) -> None:
        subprocess.run(
            ["hyprwhspr", "record", "stop"],
            capture_output=True,
            check=False,
        )

    def cancel(self) -> None:
        subprocess.run(
            ["hyprwhspr", "record", "cancel"],
            capture_output=True,
            check=False,
        )

    def capture(self) -> "subprocess.Popen[bytes]":
        return subprocess.Popen(
            ["hyprwhspr", "record", "capture"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

class Command(StrEnum):
    STATUS = "status"
    STOP = "stop"
    CANCEL = "cancel"

class Phase(StrEnum):
    RECORDING = "recording"
    WORKING = "working"
    OUTPUT = "output"

@dataclass
class Request:
    cmd: Command

@dataclass
class SessionState:
    phase: Phase
    output: OutputMode
    enrich: Optional[EnrichProvider] = None

@dataclass
class Response:
    ok: bool
    state: Optional[SessionState] = None
    error: Optional[str] = None

    @classmethod
    def from_json(cls, raw: str) -> "Response":
        obj = json.loads(raw)
        state = None
        sd = obj.get("state")
        if sd:
            enrich_val = sd.get("enrich")
            state = SessionState(
                phase=Phase(sd["phase"]),
                output=OutputMode(sd["output"]),
                enrich=EnrichProvider(enrich_val) if enrich_val else None,
            )

        return cls(
            ok=bool(obj.get("ok", False)),
            state=state,
            error=obj.get("error"),
        )

log = logging.getLogger("speech")

ICON = "/usr/share/icons/Adwaita/scalable/devices/microphone.svg"
SOCKET_PATH = os.path.join(
    os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}",
    "wayland-speech.sock",
)

AI_SYSTEM_PROMPT = load_prompt("speech.md", relative_to=__file__)
AI_USER_PROMPT = "Clean up the following speech transcription:\n<transcription>\n{text}\n</transcription>"

def _send(cmd: Command) -> Optional[Response]:
    """Deliver a command to the running session over the Unix socket.
    Returns the parsed Response, or None when no session answers."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(2)
    try:
        sock.connect(SOCKET_PATH)
    except (FileNotFoundError, ConnectionRefusedError):
        # Stale socket file from a crashed session — remove so the next
        # press 1 can bind fresh.
        try:
            os.unlink(SOCKET_PATH)
        except FileNotFoundError:
            pass
        return None
    except OSError as e:
        log.warning("socket connect failed: %s", e)
        return None

    try:
        payload = json.dumps(asdict(Request(cmd=cmd))) + "\n"
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
            return Response.from_json(raw)
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            log.warning("bad response from session: %s (raw=%r)", e, raw)
            return None
    finally:
        sock.close()

class Session:
    """Owns the Unix socket for a live session. The main thread updates
    `self.state` as it moves through phases; a background thread answers
    status/stop/cancel queries from other speech.py invocations."""

    def __init__(
        self,
        output: OutputMode,
        enrich: Optional[EnrichProvider],
        adapter: STTAdapter,
    ):
        self.state = SessionState(phase=Phase.RECORDING, output=output, enrich=enrich)
        self._adapter = adapter
        self._lock = threading.Lock()
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None

    def start(self):
        try:
            os.unlink(SOCKET_PATH)
        except FileNotFoundError:
            pass
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(SOCKET_PATH)
        os.chmod(SOCKET_PATH, 0o600)
        self._sock.listen(4)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        self._signal_waybar()

    @staticmethod
    def _signal_waybar():
        signal_waybar("speech")

    def _serve(self):
        assert self._sock is not None, "_serve requires start() to have run"
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket):
        try:
            raw = conn.recv(1024).decode("utf-8", errors="replace").strip()
            response = self._dispatch(raw)
            conn.sendall(json.dumps(asdict(response)).encode())
        except Exception as e:
            log.warning("socket handler error: %s", e)
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _dispatch(self, raw: str) -> Response:
        try:
            obj = json.loads(raw) if raw else {}
            cmd = Command(obj.get("cmd", ""))
        except (json.JSONDecodeError, ValueError):
            return Response(ok=False, error=f"bad request: {raw!r}")

        log.info("socket cmd: %s", cmd.value)
        if cmd is Command.STATUS:
            with self._lock:
                return Response(ok=True, state=SessionState(**asdict(self.state)))
        if cmd is Command.STOP:
            self._adapter.stop()
            return Response(ok=True)
        if cmd is Command.CANCEL:
            self._adapter.cancel()
            return Response(ok=True)

        return Response(ok=False, error=f"unhandled command: {cmd.value}")

    def set_phase(self, phase: Phase):
        with self._lock:
            self.state.phase = phase
        self._signal_waybar()

    def stop(self):
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
        self._signal_waybar()

class Speech:
    def __init__(
        self,
        args,
        adapter: STTAdapter,
        enricher: Optional[EnrichAdapter] = None,
        output: Optional[OutputAdapter] = None,
    ):
        self.args = args
        self._adapter = adapter
        self._enricher = enricher
        self._output = output

    def run(self):
        cmd = self.args.command
        if cmd == "toggle":
            self._toggle()
        elif cmd == "stop":
            self._stop()
        elif cmd == "kill":
            self._kill()
        elif cmd == "status":
            print(self._get_status_json())
        elif cmd == "is-recording":
            sys.exit(0 if self._is_recording() else 1)

    def _notify(self, message, timeout=None):
        notify("Speech-to-Text", message, ICON, timeout)

    def _is_recording(self):
        if _send(Command.STATUS) is not None:
            return True

        return self._adapter.is_recording()

    def _toggle(self):
        if _send(Command.STOP) is not None:
            # Press 2: a session is live. It handles the rest (enrich,
            # output, exit). Session signals waybar itself at each phase
            # transition, so press-2 has nothing to signal.
            log.info("signaled running session to stop")

            return

        # Press 1: no session. Own it.
        assert self._output is not None, "toggle requires an output adapter"
        output_mode = self._output.mode
        enrich_provider = self._enricher.provider if self._enricher else None
        log.info(
            "starting session (output=%s, enrich=%s)",
            output_mode.value,
            enrich_provider.value if enrich_provider else None,
        )

        server = Session(output_mode, enrich_provider, self._adapter)
        server.start()
        try:
            # The STT adapter subscribes to the backend's transcription
            # stream: subscribing triggers recording if the backend is idle,
            # or attaches to an in-flight one. The returned process blocks
            # until the backend delivers the final transcription and closes
            # stdout (on stop/cancel).
            capture = self._adapter.capture()
            stdout, _ = capture.communicate()

            text = stdout.decode("utf-8", errors="replace").strip() if stdout else ""
            if not text:
                log.warning("empty transcription from capture socket")
                self._notify("No transcription captured")

                return

            log.info("captured %d chars from socket", len(text))

            if self._enricher is not None:
                server.set_phase(Phase.WORKING)
                if self.args.save:
                    log.info("saving raw transcription to clipboard before enrichment")
                    subprocess.run(["wl-copy"], input=text, text=True)
                self._notify("Enriching transcription...", timeout=3000)
                enriched = self._enricher.enrich(text)
                if enriched and enriched.strip():
                    text = enriched.strip()
                else:
                    self._notify("Enrichment failed, using raw transcription")

            server.set_phase(Phase.OUTPUT)
            self._output.write(text)
            if self._enricher is not None:
                self._notify("Done")
        finally:
            server.stop()

    def _stop(self):
        if _send(Command.STOP) is None:
            # No live session; forward to the backend for any orphan recording
            # started outside speech.py. No session server means nobody else
            # is going to signal waybar, so we do it here.
            self._adapter.stop()
            Session._signal_waybar()

    def _kill(self):
        if _send(Command.CANCEL) is None:
            self._adapter.cancel()
            Session._signal_waybar()

    def _get_status_json(self):
        resp = _send(Command.STATUS)
        state = resp.state if resp and resp.ok else None

        if state is None:
            if self._adapter.is_recording():
                return json.dumps(
                    {
                        "class": Phase.RECORDING.value,
                        "text": "󰍬",
                        "tooltip": "Recording speech (no session)",
                    }
                )
            return json.dumps(
                {"class": "idle", "text": "", "tooltip": "Speech-to-text ready"}
            )

        icons = {OutputMode.CLIPBOARD: "󰅇", OutputMode.TYPE: "󰌌"}
        labels = {OutputMode.CLIPBOARD: "clipboard", OutputMode.TYPE: "typing"}
        icon = icons[state.output]
        label = labels[state.output]

        enrich_icon = " 󰧑" if state.enrich else ""
        enrich_label = f" ({state.enrich.value})" if state.enrich else ""

        status_map = {
            Phase.RECORDING: (
                f"󰍬{enrich_icon} {icon}",
                f"Recording speech{enrich_label} → {label}",
            ),
            Phase.WORKING: (
                f"󰍬{enrich_icon} {icon}",
                f"Processing transcription{enrich_label} → {label}",
            ),
            Phase.OUTPUT: (
                icon,
                f"Outputting transcription{enrich_label} → {label}",
            ),
        }
        text, tooltip = status_map[state.phase]

        return json.dumps(
            {"class": state.phase.value, "text": text, "tooltip": tooltip}
        )

def main():
    parser = argparse.ArgumentParser(
        description="Control speech-to-text via an STT adapter"
    )
    parser.add_argument("-v", "--verbose", action="store_true")

    subparsers = parser.add_subparsers(dest="command", required=True)

    toggle_parser = subparsers.add_parser("toggle")
    toggle_parser.add_argument(
        "output",
        choices=["clipboard", "type"],
        help="Output mode: 'clipboard' (wl-copy) or 'type' (paste via hyprwhspr)",
    )
    toggle_parser.add_argument(
        "--enrich",
        action="store_true",
        help="Enrich transcription through AI",
    )
    toggle_parser.add_argument(
        "--enrich-provider",
        choices=["http", "claude", "codex"],
        default="http",
    )
    toggle_parser.add_argument(
        "--enrich-base-url",
        default="https://ai.kilic.dev/api/v1",
    )
    toggle_parser.add_argument("--enrich-model", default=DEFAULT_MODEL)
    toggle_parser.add_argument("--enrich-temperature", type=float)
    toggle_parser.add_argument("--enrich-top-p", type=float)
    toggle_parser.add_argument(
        "--enrich-thinking",
        nargs="?",
        const="high",
        default="none",
        choices=["high", "medium", "low", "none"],
    )
    toggle_parser.add_argument("--enrich-num-ctx", type=int)
    toggle_parser.add_argument(
        "--save",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Before AI enrichment, copy the raw transcription to the clipboard as a backup (default: True)",
    )

    subparsers.add_parser("stop")
    subparsers.add_parser("kill")
    subparsers.add_parser("status")
    subparsers.add_parser("is-recording")

    args = parser.parse_args()

    logging.basicConfig(
        format="%(name)s: %(message)s",
        level=logging.DEBUG if args.verbose else logging.WARNING,
    )

    enricher: Optional[EnrichAdapter] = None
    if getattr(args, "enrich", False):
        match EnrichProvider(args.enrich_provider):
            case EnrichProvider.HTTP:
                enricher = HttpEnrichAdapter(
                    system_prompt=AI_SYSTEM_PROMPT,
                    user_prompt_template=AI_USER_PROMPT,
                    base_url=args.enrich_base_url,
                    model=args.enrich_model,
                    api_key=os.environ.get("AI_KILIC_DEV_API_KEY", ""),
                    temperature=args.enrich_temperature,
                    top_p=args.enrich_top_p,
                    thinking=args.enrich_thinking,
                    num_ctx=args.enrich_num_ctx,
                    user_agent="speech/1.0",
                )
            case EnrichProvider.CLAUDE:
                enricher = ClaudeEnrichAdapter(AI_SYSTEM_PROMPT, AI_USER_PROMPT)
            case EnrichProvider.CODEX:
                enricher = CodexEnrichAdapter(AI_SYSTEM_PROMPT, AI_USER_PROMPT)

    output: Optional[OutputAdapter] = None
    if hasattr(args, "output"):
        match OutputMode(args.output):
            case OutputMode.CLIPBOARD:
                output = ClipboardOutputAdapter()
            case OutputMode.TYPE:
                output = TypeOutputAdapter()

    Speech(args, HyprwhsprAdapter(), enricher, output).run()

if __name__ == "__main__":
    main()
