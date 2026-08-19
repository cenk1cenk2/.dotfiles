#!/usr/bin/env -S sh -c 'exec uv run --project "$(dirname "$0")" "$0" "$@"'

from __future__ import annotations

import io
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

import click

from lib import (
    DEFAULT_API_KEY_ENV,
    DEFAULT_BASE_URL,
    DEFAULT_ENRICH_ADAPTER,
    DEFAULT_MAX_CHARS,
    DEFAULT_PLAYER,
    DEFAULT_SAMPLE_RATE,
    DEFAULT_TIMEOUT,
    DEFAULT_TTS_TIMEOUT,
    DEFAULT_VOICE,
    EnrichAdapter,
    EnrichProvider,
    EnrichSpec,
    InputAdapter,
    InputAdapterClipboard,
    InputAdapterStdin,
    InputMode,
    OutputAdapter,
    OutputAdapterClipboard,
    OutputAdapterStdout,
    OutputAdapterType,
    OutputMode,
    PlayerAdapter,
    PlayerAdapterFfplay,
    PlayerAdapterPaplay,
    PlayerAdapterPwCat,
    PlayerMode,
    ResponseFormat,
    SynthAdapterHttp,
    TeeReader,
    TtsSpec,
    build_enricher,
    copy_audio,
    create_logger,
    load_prompt,
    notify,
    signal_waybar,
)


class STTAdapter(Protocol):
    """Speech-to-text backend contract."""

    def is_recording(self) -> bool: ...

    def stop(self) -> None: ...

    def cancel(self) -> None: ...

    def capture(self) -> subprocess.Popen[bytes]: ...


class HyprwhsprAdapter:
    def is_recording(self) -> bool:
        result = subprocess.run(
            ["hyprwhspr", "record", "status"],
            capture_output=True,
            text=True,
            check=False,
        )
        return "Recording in progress" in (result.stdout + result.stderr)

    def stop(self) -> None:
        subprocess.run(["hyprwhspr", "record", "stop"], capture_output=True, check=False)

    def cancel(self) -> None:
        subprocess.run(["hyprwhspr", "record", "cancel"], capture_output=True, check=False)

    def capture(self) -> subprocess.Popen[bytes]:
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
    def from_json(cls, raw: str) -> Response:
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
        return cls(ok=bool(obj.get("ok", False)), state=state, error=obj.get("error"))


@dataclass(frozen=True)
class SpeechPaths:
    socket_path: str
    suffix: str = ""

    @classmethod
    def from_suffix(cls, suffix: str) -> SpeechPaths:
        suffix = suffix or ""
        runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        stem = f"wayland-speech-{suffix}" if suffix else "wayland-speech"
        return cls(socket_path=os.path.join(runtime, f"{stem}.sock"), suffix=suffix)


# Populated by the `stt` click callback once `--session` is known.
_PATHS: SpeechPaths = SpeechPaths.from_suffix("")

log = logging.getLogger("speech.rpc")


def _recv_request(conn: socket.socket) -> str:
    """Read one newline-framed request.

    The enrich spec travels in this payload and grows with every field
    added to it, so a single fixed recv() would eventually truncate."""
    buf = b""
    while b"\n" not in buf:
        data = conn.recv(4096)
        if not data:
            break
        buf += data

    return buf.decode("utf-8", errors="replace").strip()


def _rpc(socket_path: str, cmd: str, **kwargs) -> Optional[str]:
    """Send one newline-framed command to `socket_path`; return the raw reply.

    None means nothing answered — no session, a stale socket (which is
    unlinked on the way out), or a dropped reply. Callers read all three
    as "no session listening"."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(2)
    try:
        sock.connect(socket_path)
    except (FileNotFoundError, ConnectionRefusedError):
        try:
            os.unlink(socket_path)
        except FileNotFoundError:
            pass
        return None
    except OSError as e:
        log.warning("socket connect failed: %s", e)
        return None

    try:
        payload = json.dumps({"cmd": cmd, **kwargs}) + "\n"
        log.debug("rpc send: %s", payload.rstrip())
        sock.sendall(payload.encode())
        chunks = []
        while True:
            data = sock.recv(4096)
            if not data:
                break
            chunks.append(data)
        raw = b"".join(chunks).decode("utf-8", errors="replace").strip()
        return raw or None
    finally:
        sock.close()


class Session:
    """UNIX-socket-backed live recording session."""

    log = logging.getLogger("speech.session")

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
        with self._lock:
            self.enricher = enricher
            self.state.enrich = enricher.provider if enricher else None
        self._signal_waybar()

    def set_output(self, output: OutputAdapter) -> None:
        with self._lock:
            self.output = output
            self.state.output = output.mode
        self._signal_waybar()

    def start(self):
        # Session leader → KILL reaches every child subprocess.
        try:
            os.setpgrp()
        except OSError as e:
            self.log.warning("setpgrp failed: %s", e)

        try:
            os.unlink(_PATHS.socket_path)
        except FileNotFoundError:
            pass
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(_PATHS.socket_path)
        os.chmod(_PATHS.socket_path, 0o600)
        self._sock.listen(4)
        self.log.debug("listening on %s", _PATHS.socket_path)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        self._signal_waybar()

    @staticmethod
    def _signal_waybar():
        signal_waybar("speech")

    def _serve(self):
        assert self._sock is not None
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket):
        # A dropped reply is indistinguishable from "no session listening" to
        # the client, which answers by starting a second session that rebinds
        # the socket over the live recorder — so every path must answer.
        try:
            response = self._dispatch(_recv_request(conn))
        except Exception as e:
            self.log.warning("socket handler error: %s", e)
            response = Response(ok=False, error=str(e))
        try:
            conn.sendall(json.dumps(asdict(response)).encode())
        except OSError as e:
            self.log.warning("socket reply failed: %s", e)
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

        self.log.info("socket cmd: %s", cmd.value)
        if cmd is Command.STATUS:
            with self._lock:
                return Response(ok=True, state=SessionState(**asdict(self.state)))
        if cmd is Command.STOP:
            # Stop first: an override that raises must not leave the recorder
            # running with no second toggle able to reach it.
            self._adapter.stop()
            if "enrich" in obj:
                self._apply_enrich_override(obj["enrich"])
            if obj.get("output"):
                self._apply_output_override(OutputMode(obj["output"]))
            return Response(ok=True)
        if cmd is Command.KILL:
            self._adapter.cancel()
            os.killpg(0, signal.SIGKILL)

        return Response(ok=False, error=f"unhandled command: {cmd.value}")

    def _apply_enrich_override(self, spec_dict: Optional[dict]) -> None:
        new_enricher: Optional[EnrichAdapter] = None
        if spec_dict:
            new_enricher = build_enricher(
                EnrichSpec.from_dict(spec_dict),
                Speech.SYSTEM_PROMPT,
                Speech.USER_PROMPT,
            )
        self.set_enricher(new_enricher)

    def _apply_output_override(self, mode: OutputMode) -> None:
        match mode:
            case OutputMode.CLIPBOARD:
                self.set_output(OutputAdapterClipboard())
            case OutputMode.TYPE:
                self.set_output(OutputAdapterType())
            case OutputMode.STDOUT:
                self.set_output(OutputAdapterStdout())
            case _:
                raise ValueError(f"unsupported output mode: {mode!r}")

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
            os.unlink(_PATHS.socket_path)
        except FileNotFoundError:
            pass
        self._signal_waybar()


class Speech:
    ICON = "/usr/share/icons/Adwaita/scalable/devices/microphone.svg"
    SYSTEM_PROMPT = load_prompt("speech.md", relative_to=__file__)
    USER_PROMPT = (
        "Clean up the following speech transcription:\n"
        "<transcription>\n{text}\n</transcription>"
    )

    log = logging.getLogger("speech")

    def __init__(
        self,
        adapter: STTAdapter,
        enricher: Optional[EnrichAdapter] = None,
        output: Optional[OutputAdapter] = None,
    ):
        self._adapter = adapter
        self._enricher = enricher
        self._output = output

    # ── core ──────────────────────────────────────────────────────

    def _notify(self, message, timeout=None):
        notify("Speech-to-Text", message, self.ICON, timeout)

    @classmethod
    def _send(cls, cmd: Command, **kwargs) -> Optional[Response]:
        raw = _rpc(_PATHS.socket_path, cmd.value, **kwargs)
        if raw is None:
            return None
        try:
            return Response.from_json(raw)
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            cls.log.warning("bad response: %s (raw=%r)", e, raw)
            return None

    def is_recording(self) -> bool:
        if self._send(Command.STATUS) is not None:
            return True
        return self._adapter.is_recording()

    def run_once(
        self,
        *,
        enrich_spec: Optional[EnrichSpec],
        output_mode: OutputMode,
        save: bool,
    ) -> None:
        enrich_payload = enrich_spec.to_dict() if enrich_spec else None
        if self._send(Command.STOP, enrich=enrich_payload, output=output_mode.value) is not None:
            self.log.info("press-2: signaled running session to stop")
            return

        assert self._output is not None, "toggle requires an output adapter"
        self.log.info(
            "starting session output=%s enrich=%s",
            self._output.mode.value,
            self._enricher.provider.value if self._enricher else None,
        )

        server = Session(self._output, self._enricher, self._adapter)
        server.start()
        try:
            capture = self._adapter.capture()
            stdout, _ = capture.communicate()

            text = stdout.decode("utf-8", errors="replace").strip() if stdout else ""
            if not text:
                self.log.warning("empty transcription")
                self._notify("No transcription captured")
                return

            self.log.info("captured %d chars", len(text))

            enricher = server.enricher
            output = server.output
            if enricher is not None:
                server.set_phase(Phase.WORKING)
                if save:
                    self.log.debug("saving raw transcription to clipboard")
                    subprocess.run(["wl-copy"], input=text, text=True)
                self._notify("Enriching transcription...", timeout=3000)
                enriched = enricher.enrich(text)
                if enriched and enriched.strip():
                    text = enriched.strip()
                else:
                    self.log.warning("enrichment empty; using raw")
                    self._notify("Enrichment failed, using raw transcription")

            server.set_phase(Phase.OUTPUT)
            output.write(text)
            if enricher is not None:
                self._notify("Done")
        finally:
            server.stop()

    def stop(self):
        if self._send(Command.STOP) is None:
            self._adapter.stop()
            Session._signal_waybar()

    def kill(self):
        if self._send(Command.KILL) is None:
            self._adapter.cancel()
            Session._signal_waybar()

    def status_json(self) -> str:
        resp = self._send(Command.STATUS)
        state = resp.state if resp and resp.ok else None

        if state is None:
            if self._adapter.is_recording():
                return json.dumps(
                    {"class": Phase.RECORDING.value, "text": "󰍬", "tooltip": "Recording (no session)"}
                )
            return json.dumps({"class": "idle", "text": "", "tooltip": "Speech-to-text ready"})

        icons = {OutputMode.CLIPBOARD: "󰅇", OutputMode.TYPE: "󰌌", OutputMode.STDOUT: "󰼭"}
        labels = {OutputMode.CLIPBOARD: "clipboard", OutputMode.TYPE: "typing", OutputMode.STDOUT: "stdout"}
        icon = icons[state.output]
        label = labels[state.output]
        enrich_label = f" ({state.enrich.value})" if state.enrich else ""

        mapping = {
            Phase.RECORDING: (f"󰍬 {icon}", f"Recording speech → {label}"),
            Phase.WORKING: (f"󰼭 󰧑 {icon}", f"Processing{enrich_label} → {label}"),
            Phase.OUTPUT: (icon, f"Outputting → {label}"),
        }
        text, tooltip = mapping[state.phase]
        return json.dumps({"class": state.phase.value, "text": text, "tooltip": tooltip})

    # ── CLI ───────────────────────────────────────────────────────

    @click.group()
    @click.option("--session", "session_suffix", default="", metavar="SUFFIX", help="Socket-path suffix.")
    def cli(session_suffix: str):
        """Control speech-to-text via an STT adapter."""
        global _PATHS
        _PATHS = SpeechPaths.from_suffix(session_suffix or "")

    @cli.command("toggle")
    @click.option(
        "--output",
        type=click.Choice([m.value for m in OutputMode], case_sensitive=False),
        default=OutputMode.CLIPBOARD.value,
        help="Output sink.",
    )
    @click.option("--enrich", is_flag=True, help="Enrich transcription through AI.")
    @click.option(
        "--enrich-provider",
        type=click.Choice([p.value for p in EnrichProvider], case_sensitive=False),
        default=DEFAULT_ENRICH_ADAPTER.value,
    )
    @click.option("--enrich-base-url", default=DEFAULT_BASE_URL)
    @click.option(
        "--enrich-api-key-env",
        default=DEFAULT_API_KEY_ENV,
        help="Env var holding the HTTP backend key.",
    )
    @click.option(
        "--enrich-model",
        default=None,
        help="Model id, or hyprpilot profile id.",
    )
    @click.option("--enrich-temperature", type=float, default=None)
    @click.option("--enrich-top-p", type=float, default=None)
    @click.option(
        "--enrich-thinking",
        type=click.Choice(["high", "medium", "low", "none"]),
        default="none",
    )
    @click.option("--enrich-num-ctx", type=int, default=None)
    @click.option(
        "--enrich-timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help="Backend deadline in seconds.",
    )
    @click.option("--save/--no-save", default=True, help="Copy raw transcript to clipboard first.")
    def cmd_toggle(
        output,
        enrich,
        enrich_provider,
        enrich_base_url,
        enrich_api_key_env,
        enrich_model,
        enrich_temperature,
        enrich_top_p,
        enrich_thinking,
        enrich_num_ctx,
        enrich_timeout,
        save,
    ):
        """Start a session, or toggle an existing one."""
        output_mode = OutputMode(output)
        match output_mode:
            case OutputMode.CLIPBOARD:
                output_adapter: OutputAdapter = OutputAdapterClipboard()
            case OutputMode.TYPE:
                output_adapter = OutputAdapterType()
            case OutputMode.STDOUT:
                output_adapter = OutputAdapterStdout()
            case _:
                raise click.UsageError(f"unsupported output mode: {output_mode!r}")

        enrich_spec: Optional[EnrichSpec] = None
        enricher: Optional[EnrichAdapter] = None
        if enrich:
            enrich_spec = EnrichSpec(
                provider=EnrichProvider(enrich_provider),
                model=enrich_model,
                timeout=enrich_timeout,
                base_url=enrich_base_url,
                api_key_env=enrich_api_key_env,
                temperature=enrich_temperature,
                top_p=enrich_top_p,
                thinking=enrich_thinking,
                num_ctx=enrich_num_ctx,
                user_agent="speech/1.0",
            )
            enricher = build_enricher(
                enrich_spec, Speech.SYSTEM_PROMPT, Speech.USER_PROMPT
            )

        Speech(HyprwhsprAdapter(), enricher, output_adapter).run_once(
            enrich_spec=enrich_spec,
            output_mode=output_mode,
            save=save,
        )

    @cli.command("stop")
    def cmd_stop():
        """Stop the active session."""
        Speech(HyprwhsprAdapter()).stop()

    @cli.command("kill")
    def cmd_kill():
        """Kill the session's process group."""
        Speech(HyprwhsprAdapter()).kill()

    @cli.command("status")
    def cmd_status():
        """Print waybar-shaped status JSON."""
        sys.stdout.write(Speech(HyprwhsprAdapter()).status_json() + "\n")

    @cli.command("is-recording")
    def cmd_is_recording():
        """Exit 0 if a recording is live."""
        sys.exit(0 if Speech(HyprwhsprAdapter()).is_recording() else 1)


class TtsPhase(StrEnum):
    FETCHING = "fetching"
    SPEAKING = "speaking"


@dataclass
class TtsState:
    phase: TtsPhase
    voice: str
    chars: int


@dataclass
class TtsResponse:
    ok: bool
    state: Optional[TtsState] = None
    error: Optional[str] = None

    @classmethod
    def from_json(cls, raw: str) -> TtsResponse:
        obj = json.loads(raw)
        state = None
        sd = obj.get("state")
        if sd:
            state = TtsState(
                phase=TtsPhase(sd["phase"]),
                voice=sd["voice"],
                chars=int(sd["chars"]),
            )
        return cls(ok=bool(obj.get("ok", False)), state=state, error=obj.get("error"))


@dataclass(frozen=True)
class TtsPaths:
    socket_path: str
    suffix: str = ""

    @classmethod
    def from_suffix(cls, suffix: str) -> TtsPaths:
        suffix = suffix or ""
        runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        stem = f"wayland-tts-{suffix}" if suffix else "wayland-tts"
        return cls(socket_path=os.path.join(runtime, f"{stem}.sock"), suffix=suffix)


# Populated by the `tts` click callback once `--session` is known.
_TTS_PATHS: TtsPaths = TtsPaths.from_suffix("")


class TtsSession:
    """UNIX-socket-backed live playback session."""

    WAYBAR_MODULE = "speech-tts"

    log = logging.getLogger("speech.tts.session")

    def __init__(self, voice: str, chars: int):
        self.state = TtsState(phase=TtsPhase.FETCHING, voice=voice, chars=chars)
        self._lock = threading.Lock()
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None

    def start(self):
        # Session leader → KILL reaches the player subprocess too.
        try:
            os.setpgrp()
        except OSError as e:
            self.log.warning("setpgrp failed: %s", e)

        try:
            os.unlink(_TTS_PATHS.socket_path)
        except FileNotFoundError:
            pass
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(_TTS_PATHS.socket_path)
        os.chmod(_TTS_PATHS.socket_path, 0o600)
        self._sock.listen(4)
        self.log.debug("listening on %s", _TTS_PATHS.socket_path)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        self._signal_waybar()

    @classmethod
    def _signal_waybar(cls):
        signal_waybar(cls.WAYBAR_MODULE)

    def _serve(self):
        assert self._sock is not None
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket):
        # A dropped reply reads as "no session listening" to the client, which
        # answers by starting a second one over the live playback — so every
        # path must answer.
        try:
            response = self._dispatch(_recv_request(conn))
        except Exception as e:
            self.log.warning("socket handler error: %s", e)
            response = TtsResponse(ok=False, error=str(e))
        try:
            conn.sendall(json.dumps(asdict(response)).encode())
        except OSError as e:
            self.log.warning("socket reply failed: %s", e)
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _dispatch(self, raw: str) -> TtsResponse:
        try:
            obj = json.loads(raw) if raw else {}
            cmd = Command(obj.get("cmd", ""))
        except (json.JSONDecodeError, ValueError):
            return TtsResponse(ok=False, error=f"bad request: {raw!r}")

        self.log.info("socket cmd: %s", cmd.value)
        if cmd is Command.STATUS:
            with self._lock:
                return TtsResponse(ok=True, state=TtsState(**asdict(self.state)))
        if cmd is Command.KILL:
            os.killpg(0, signal.SIGKILL)

        return TtsResponse(ok=False, error=f"unhandled command: {cmd.value}")

    def set_phase(self, phase: TtsPhase):
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
            os.unlink(_TTS_PATHS.socket_path)
        except FileNotFoundError:
            pass
        self._signal_waybar()


class Tts:
    ICON = "/usr/share/icons/Adwaita/symbolic/legacy/multimedia-volume-control-symbolic.svg"
    # wl-paste also advertises the legacy X11 selection atoms, and some
    # toolkits offer nothing else for plain text.
    TEXT_ATOMS = ("UTF8_STRING", "STRING", "TEXT")

    log = logging.getLogger("speech.tts")

    def __init__(
        self,
        spec: Optional[TtsSpec] = None,
        input: Optional[InputAdapter] = None,
        player: Optional[PlayerAdapter] = None,
        copy: bool = False,
    ):
        self._spec = spec or TtsSpec()
        self._input = input
        self._player = player
        self._copy = copy

    # ── core ──────────────────────────────────────────────────────

    def _notify(self, message, timeout=None):
        notify("Text-to-Speech", message, self.ICON, timeout)

    @classmethod
    def _send(cls, cmd: Command, **kwargs) -> Optional[TtsResponse]:
        raw = _rpc(_TTS_PATHS.socket_path, cmd.value, **kwargs)
        if raw is None:
            return None
        try:
            return TtsResponse.from_json(raw)
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            cls.log.warning("bad response: %s (raw=%r)", e, raw)
            return None

    def is_speaking(self) -> bool:
        return self._send(Command.STATUS) is not None

    def _read_text(self) -> Optional[str]:
        """Text to speak, or None with the user already notified why."""
        assert self._input is not None
        if self._input.mode is InputMode.CLIPBOARD:
            types = InputAdapterClipboard.list_mime_types()
            if not any(
                t.startswith("text/") or t in self.TEXT_ATOMS for t in types
            ):
                self.log.warning("clipboard holds no text: %s", types)
                self._notify("Clipboard holds no text")
                return None

        text = self._input.read()
        if not text or not text.strip():
            self.log.warning("%s was empty", self._input.mode.value)
            self._notify(f"{self._input.mode.value.capitalize()} is empty")
            return None

        text = text.strip()
        if len(text) > self._spec.max_chars:
            self.log.warning(
                "truncating %d chars to %d", len(text), self._spec.max_chars
            )
            self._notify(f"Truncated to {self._spec.max_chars} characters")
            text = text[: self._spec.max_chars]
        return text

    def speak(self) -> None:
        assert self._input is not None, "speak requires an input adapter"
        assert self._player is not None, "speak requires a player adapter"
        spec = self._spec

        # Refuse rather than rebind: a second bind would drop the running
        # session's socket and leave its player unreachable to `kill`.
        if self.is_speaking():
            self.log.info("a session is already speaking; bailing")
            self._notify("Already speaking")
            return

        text = self._read_text()
        if text is None:
            return

        self.log.info("speaking %d chars (voice=%s)", len(text), spec.voice)
        session = TtsSession(spec.voice, len(text))
        session.start()
        try:
            buffer = io.BytesIO() if self._copy else None
            try:
                with SynthAdapterHttp(spec).synth(text) as stream:
                    session.set_phase(TtsPhase.SPEAKING)
                    source = TeeReader(stream, buffer) if buffer else stream
                    written = self._player.play(source, spec.sample_rate)
            except Exception as e:
                self.log.error("synthesis failed: %s", e)
                self._notify("Synthesis failed")
                return

            if not written:
                self.log.warning("backend returned no audio")
                self._notify("No audio returned")
                return

            self.log.info("played %d bytes through %s", written, self._player.mode.value)
            if buffer is not None:
                copy_audio(buffer.getvalue(), spec)
                self._notify("Audio copied to clipboard", timeout=3000)
        finally:
            session.stop()

    def kill(self) -> None:
        # No reply is the *expected* outcome: the session killpg's itself
        # before it can answer, so there is nothing here to branch on.
        self._send(Command.KILL)
        TtsSession._signal_waybar()

    def status_json(self) -> str:
        resp = self._send(Command.STATUS)
        state = resp.state if resp and resp.ok else None

        if state is None:
            return json.dumps(
                {"class": "idle", "text": "", "tooltip": "Text-to-speech ready"}
            )

        mapping = {
            TtsPhase.FETCHING: (
                "󰧑 󰕾",
                f"Synthesizing {state.chars} chars ({state.voice})",
            ),
            TtsPhase.SPEAKING: (
                "󰕾",
                f"Speaking {state.chars} chars ({state.voice})",
            ),
        }
        text, tooltip = mapping[state.phase]
        return json.dumps({"class": state.phase.value, "text": text, "tooltip": tooltip})

    # ── CLI ───────────────────────────────────────────────────────

    @click.group()
    @click.option("--session", "session_suffix", default="", metavar="SUFFIX", help="Socket-path suffix.")
    def cli(session_suffix: str):
        """Read text aloud through a TTS backend."""
        global _TTS_PATHS
        _TTS_PATHS = TtsPaths.from_suffix(session_suffix or "")

    @cli.command("speak")
    @click.option(
        "--input",
        "input_",
        type=click.Choice([m.value for m in InputMode], case_sensitive=False),
        default=InputMode.CLIPBOARD.value,
        help="Text source.",
    )
    @click.option("--voice", default=DEFAULT_VOICE, help="Backend voice id.")
    @click.option("--model", default=None, help="TTS model id.")
    @click.option("--speed", type=float, default=1.0, help="Speaking rate multiplier.")
    @click.option(
        "--format",
        "response_format",
        type=click.Choice([f.value for f in ResponseFormat], case_sensitive=False),
        default=ResponseFormat.PCM.value,
        help="Audio encoding.",
    )
    @click.option(
        "--sample-rate",
        type=int,
        default=DEFAULT_SAMPLE_RATE,
        help="Output rate in Hz.",
    )
    @click.option(
        "--player",
        type=click.Choice([p.value for p in PlayerMode], case_sensitive=False),
        default=DEFAULT_PLAYER.value,
        help="Playback sink.",
    )
    @click.option("--base-url", default=DEFAULT_BASE_URL, help="HTTP backend base URL.")
    @click.option(
        "--api-key-env",
        default=DEFAULT_API_KEY_ENV,
        help="Env var holding the HTTP backend key.",
    )
    @click.option(
        "--timeout",
        type=float,
        default=DEFAULT_TTS_TIMEOUT,
        help="Backend deadline in seconds.",
    )
    @click.option(
        "--max-chars",
        type=int,
        default=DEFAULT_MAX_CHARS,
        help="Truncate longer input.",
    )
    @click.option("--copy/--no-copy", default=False, help="Copy the audio to the clipboard.")
    def cmd_speak(
        input_,
        voice,
        model,
        speed,
        response_format,
        sample_rate,
        player,
        base_url,
        api_key_env,
        timeout,
        max_chars,
        copy,
    ):
        """Synthesize the input text and play it."""
        input_mode = InputMode(input_)
        match input_mode:
            case InputMode.CLIPBOARD:
                input_adapter: InputAdapter = InputAdapterClipboard()
            case InputMode.STDIN:
                input_adapter = InputAdapterStdin()
            case _:
                raise click.UsageError(f"unknown input mode: {input_mode!r}")

        fmt = ResponseFormat(response_format)
        player_mode = PlayerMode(player)
        if fmt is not ResponseFormat.PCM and player_mode is not PlayerMode.FFPLAY:
            raise click.UsageError(f"{player_mode.value} plays raw pcm only")

        match player_mode:
            case PlayerMode.FFPLAY:
                player_adapter: PlayerAdapter = PlayerAdapterFfplay(fmt)
            case PlayerMode.PW_CAT:
                player_adapter = PlayerAdapterPwCat()
            case PlayerMode.PAPLAY:
                player_adapter = PlayerAdapterPaplay()
            case _:
                raise click.UsageError(f"unsupported player: {player_mode!r}")

        spec = TtsSpec(
            model=model,
            voice=voice,
            speed=speed,
            response_format=fmt,
            sample_rate=sample_rate,
            base_url=base_url,
            api_key_env=api_key_env,
            timeout=timeout,
            max_chars=max_chars,
            user_agent="speech/1.0",
        )

        Tts(spec, input_adapter, player_adapter, copy).speak()

    @cli.command("toggle")
    @click.pass_context
    def cmd_toggle(ctx: click.Context):
        """Kill an active session, or speak the clipboard."""
        if Tts().is_speaking():
            Tts().kill()
            return
        # Defaults come from `speak`'s own options, so the two paths can
        # never drift.
        ctx.invoke(Tts.cmd_speak)

    @cli.command("kill")
    def cmd_kill():
        """Kill the session's process group, player included."""
        Tts().kill()

    @cli.command("status")
    def cmd_status():
        """Print waybar-shaped status JSON."""
        sys.stdout.write(Tts().status_json() + "\n")

    @cli.command("is-speaking")
    def cmd_is_speaking():
        """Exit 0 if playback is live."""
        sys.exit(0 if Tts().is_speaking() else 1)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging.")
def cli(verbose: bool):
    """Speech-to-text capture and text-to-speech playback."""
    create_logger(verbose)


cli.add_command(Speech.cli, "stt")
cli.add_command(Tts.cli, "tts")


if __name__ == "__main__":
    cli()
