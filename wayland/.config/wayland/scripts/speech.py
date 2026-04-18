#!/usr/bin/env python3

import argparse
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Optional, Protocol

from lib import (
    DEFAULT_ENRICH_ADAPTER,
    DEFAULT_ENRICH_MODEL,
    EnrichAdapterClaude,
    OutputAdapterClipboard,
    EnrichAdapterCodex,
    EnrichAdapter,
    EnrichProvider,
    EnrichAdapterHttp,
    OutputAdapter,
    OutputMode,
    OutputAdapterType,
    load_prompt,
    notify,
    signal_waybar,
    OutputAdapterStdout,
)

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
    KILL = "kill"

class Phase(StrEnum):
    RECORDING = "recording"
    WORKING = "working"
    OUTPUT = "output"

@dataclass
class EnrichSpec:
    """Serialisable form of an enrichment choice, sent over the socket.

    `provider` identifies the backend; the remaining fields are config that
    the `HttpEnrichAdapter` needs. Claude/Codex ignore everything but
    `provider`."""

    provider: EnrichProvider
    base_url: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    thinking: str = "none"
    num_ctx: Optional[int] = None

    @classmethod
    def from_dict(cls, d: dict) -> "EnrichSpec":
        return cls(
            provider=EnrichProvider(d["provider"]),
            base_url=d.get("base_url"),
            model=d.get("model"),
            api_key=d.get("api_key"),
            temperature=d.get("temperature"),
            top_p=d.get("top_p"),
            thinking=d.get("thinking", "none"),
            num_ctx=d.get("num_ctx"),
        )

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

def _send(cmd: Command, **kwargs) -> Optional[Response]:
    """Deliver a command to the running session over the Unix socket.

    Extra kwargs become top-level fields in the JSON payload. The server
    inspects `"enrich" in payload` to tell an override from a no-op — only
    the press-2 toggle path sets it, so presence is the override signal.

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
        payload = json.dumps({"cmd": cmd.value, **kwargs}) + "\n"
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
        output: OutputAdapter,
        enricher: Optional[EnrichAdapter],
        adapter: STTAdapter,
    ):
        self.state = SessionState(
            phase=Phase.RECORDING,
            output=output.mode,
            enrich=enricher.provider if enricher else None,
        )
        self.output = output
        self.enricher = enricher
        self._adapter = adapter
        self._lock = threading.Lock()
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None

    def set_enricher(self, enricher: Optional[EnrichAdapter]) -> None:
        """Swap the active enricher mid-session. Updates the state's
        displayed provider so waybar reflects the new choice."""
        with self._lock:
            self.enricher = enricher
            self.state.enrich = enricher.provider if enricher else None
        self._signal_waybar()

    def set_output(self, output: OutputAdapter) -> None:
        """Swap the active output sink mid-session. Updates the state's
        mode so waybar reflects the new destination."""
        with self._lock:
            self.output = output
            self.state.output = output.mode
        self._signal_waybar()

    def start(self):
        # Become our own process group leader so a KILL command can take out
        # every subprocess we've spawned (capture client, enrichment CLIs,
        # output commands) in one signal.
        try:
            os.setpgrp()
        except OSError as e:
            log.warning("setpgrp failed: %s; KILL may leak subprocesses", e)

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
            # Presence of an "enrich" key signals the press-2 toggle
            # overriding press-1's choice. Value is either an EnrichSpec
            # dict (→ new enricher) or null (→ skip enrichment entirely).
            # Apply the swap BEFORE telling the daemon to stop, so press-1's
            # main thread sees the new enricher when communicate() unblocks.
            if "enrich" in obj:
                spec_dict = obj["enrich"]
                new_enricher: Optional[EnrichAdapter] = None
                if spec_dict:
                    spec = EnrichSpec.from_dict(spec_dict)
                    match spec.provider:
                        case EnrichProvider.HTTP:
                            new_enricher = EnrichAdapterHttp(
                                system_prompt=AI_SYSTEM_PROMPT,
                                user_prompt_template=AI_USER_PROMPT,
                                base_url=spec.base_url or "https://ai.kilic.dev/api/v1",
                                model=spec.model or DEFAULT_ENRICH_MODEL,
                                api_key=spec.api_key or "",
                                temperature=spec.temperature,
                                top_p=spec.top_p,
                                thinking=spec.thinking,
                                num_ctx=spec.num_ctx,
                                user_agent="speech/1.0",
                            )
                        case EnrichProvider.CLAUDE:
                            new_enricher = EnrichAdapterClaude(
                                AI_SYSTEM_PROMPT,
                                AI_USER_PROMPT,
                            )
                        case EnrichProvider.CODEX:
                            new_enricher = EnrichAdapterCodex(
                                AI_SYSTEM_PROMPT,
                                AI_USER_PROMPT,
                            )
                        case _:
                            raise ValueError(
                                f"unknown enrich provider: {spec.provider!r}",
                            )
                self.set_enricher(new_enricher)
            # Presence of an "output" key signals the press-2 toggle picking
            # a different sink. Same window as the enrich override: lands
            # before the daemon transcribes, so press-1's output write goes
            # to the new destination.
            if obj.get("output"):
                mode = OutputMode(obj["output"])
                match mode:
                    case OutputMode.CLIPBOARD:
                        self.set_output(OutputAdapterClipboard())
                    case OutputMode.TYPE:
                        self.set_output(OutputAdapterType())
                    case OutputMode.STDOUT:
                        self.set_output(OutputAdapterStdout())
                    case _:
                        raise ValueError(
                            f"unsupported output mode for speech: {mode!r}",
                        )
            self._adapter.stop()
            return Response(ok=True)
        if cmd is Command.KILL:
            # Cancel the daemon so it isn't left transcribing into a dead
            # socket, then SIGKILL our process group. This does not return
            # — the client sees EOF on its socket instead of a response.
            self._adapter.cancel()
            os.killpg(0, signal.SIGKILL)

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
        # Press 2 path: ship this invocation's enrich + output choices
        # alongside STOP so the running session can swap them before the
        # daemon starts transcribing. Both are read only when present in
        # the payload — absent keys leave press-1's setup intact.
        enrich_payload = None
        if getattr(self.args, "enrich", False):
            enrich_payload = asdict(
                EnrichSpec(
                    provider=EnrichProvider(self.args.enrich_provider),
                    base_url=self.args.enrich_base_url,
                    model=self.args.enrich_model,
                    api_key=os.environ.get("AI_KILIC_DEV_API_KEY", ""),
                    temperature=self.args.enrich_temperature,
                    top_p=self.args.enrich_top_p,
                    thinking=self.args.enrich_thinking,
                    num_ctx=self.args.enrich_num_ctx,
                )
            )
        output_payload = self.args.output.value
        if (
            _send(
                Command.STOP,
                enrich=enrich_payload,
                output=output_payload,
            )
            is not None
        ):
            log.info("signaled running session to stop")

            return

        # Press 1: no session. Own it.
        assert self._output is not None, "toggle requires an output adapter"
        log.info(
            "starting session (output=%s, enrich=%s)",
            self._output.mode.value,
            self._enricher.provider.value if self._enricher else None,
        )

        server = Session(self._output, self._enricher, self._adapter)
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

            # Read enricher + output from the session — the dispatch thread
            # may have swapped either on STOP with press-2's choices.
            enricher = server.enricher
            output = server.output
            if enricher is not None:
                server.set_phase(Phase.WORKING)
                if self.args.save:
                    log.info("saving raw transcription to clipboard before enrichment")
                    subprocess.run(["wl-copy"], input=text, text=True)
                self._notify("Enriching transcription...", timeout=3000)
                enriched = enricher.enrich(text)
                if enriched and enriched.strip():
                    text = enriched.strip()
                else:
                    self._notify("Enrichment failed, using raw transcription")

            server.set_phase(Phase.OUTPUT)
            output.write(text)
            if enricher is not None:
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
        if _send(Command.KILL) is None:
            # No live session — nothing to tear down, but the daemon may
            # still be recording via some other entry point, so cancel it.
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

        icons = {
            OutputMode.CLIPBOARD: "󰅇",
            OutputMode.TYPE: "󰌌",
            OutputMode.STDOUT: "󰸾",
        }
        labels = {
            OutputMode.CLIPBOARD: "clipboard",
            OutputMode.TYPE: "typing",
            OutputMode.STDOUT: "stdout",
        }
        icon = icons[state.output]
        label = labels[state.output]

        # RECORDING is ambiguous: press-2 may still swap or drop the enricher,
        # so we don't claim a provider yet. By WORKING the swap has landed;
        # by OUTPUT the transcription is out the door.
        enrich_label = f" ({state.enrich.value})" if state.enrich else ""

        status_map = {
            Phase.RECORDING: (
                f"󰍬 {icon}",
                f"Recording speech → {label}",
            ),
            Phase.WORKING: (
                f"󰼭 󰧑 {icon}",
                f"Processing transcription{enrich_label} → {label}",
            ),
            Phase.OUTPUT: (
                icon,
                f"Outputting transcription → {label}",
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
        "--output",
        type=OutputMode,
        choices=list(OutputMode),
        default=OutputMode.CLIPBOARD,
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
        default=DEFAULT_ENRICH_ADAPTER,
    )
    toggle_parser.add_argument(
        "--enrich-base-url",
        default="https://ai.kilic.dev/api/v1",
    )
    toggle_parser.add_argument("--enrich-model", default=DEFAULT_ENRICH_MODEL)
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
        provider = EnrichProvider(args.enrich_provider)
        match provider:
            case EnrichProvider.HTTP:
                enricher = EnrichAdapterHttp(
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
                enricher = EnrichAdapterClaude(AI_SYSTEM_PROMPT, AI_USER_PROMPT)
            case EnrichProvider.CODEX:
                enricher = EnrichAdapterCodex(AI_SYSTEM_PROMPT, AI_USER_PROMPT)
            case _:
                raise ValueError(f"unknown enrich provider: {provider!r}")

    output: Optional[OutputAdapter] = None
    if hasattr(args, "output"):
        match args.output:
            case OutputMode.CLIPBOARD:
                output = OutputAdapterClipboard()
            case OutputMode.TYPE:
                output = OutputAdapterType()
            case OutputMode.STDOUT:
                output = OutputAdapterStdout()
            case _:
                raise ValueError(
                    f"unsupported output mode for speech: {args.output!r}",
                )

    Speech(args, HyprwhsprAdapter(), enricher, output).run()

if __name__ == "__main__":
    main()
