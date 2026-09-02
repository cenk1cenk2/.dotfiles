#!/usr/bin/env -S sh -c 'exec uv run --project "$(dirname "$0")" "$0" "$@"'

from __future__ import annotations

import http.client
import io
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import urllib.error
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import click
from dotlib.cli import (
    create_logger,
)
from dotlib.desktop import (
    is_headless,
    set_headless,
)
from dotlib.notify import (
    notify,
)
from lib import (
    DEFAULT_API_KEY_ENV,
    DEFAULT_BASE_URL,
    DEFAULT_STT_LANGUAGE,
    DEFAULT_STT_TIMEOUT,
    DEFAULT_TTS_PLAYER,
    DEFAULT_TTS_SAMPLE_RATE,
    DEFAULT_TTS_TIMEOUT,
    DEFAULT_TTS_VOICE,
    PLAIN_FORMATS,
    AudioFormat,
    EnrichAdapter,
    EnrichProvider,
    EnrichSpec,
    InputAdapter,
    InputAdapterClipboard,
    InputMode,
    OutputAdapter,
    OutputAdapterClipboard,
    OutputMode,
    PlayerAdapter,
    PlayerAdapterFfplay,
    PlayerAdapterPaplay,
    PlayerAdapterPwCat,
    PlayerMode,
    ResponseFormat,
    SttAdapter,
    SttAdapterHttp,
    SttAdapterHyprwhspr,
    SttProvider,
    SttRecorder,
    SttSpec,
    TeeReader,
    TtsAdapterHttp,
    TtsSpec,
    build_enricher,
    build_input,
    build_output,
    copy_audio,
    enrich_options,
    load_prompt,
    signal_waybar,
    spec_from_options,
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
    enrich: EnrichProvider | None = None

@dataclass
class Response:
    ok: bool
    state: SessionState | None = None
    error: str | None = None

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
class SttPaths:
    socket_path: str
    suffix: str = ""

    @classmethod
    def from_suffix(cls, suffix: str) -> SttPaths:
        suffix = suffix or ""
        runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        stem = f"wayland-stt-{suffix}" if suffix else "wayland-stt"
        return cls(socket_path=os.path.join(runtime, f"{stem}.sock"), suffix=suffix)

# Populated by the `stt` click callback once `--session` is known.
_STT_PATHS: SttPaths = SttPaths.from_suffix("")

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

def _rpc(socket_path: str, cmd: str, **kwargs) -> str | None:
    """Send one newline-framed command to `socket_path`; return the raw reply.

    None means nothing answered — no session, a stale socket (which is
    unlinked on the way out), or a dropped reply. Callers read all three
    as "no session listening"."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(2)
    try:
        sock.connect(socket_path)
    except FileNotFoundError, ConnectionRefusedError:
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

def _pairs(values: tuple[str, ...], flag: str) -> tuple[tuple[str, str], ...]:
    """Parse repeated `name=value` arguments, keeping order and duplicates."""
    parsed = []
    for value in values:
        name, sep, rest = value.partition("=")
        if not sep or not name:
            raise click.UsageError(f"{flag} takes name=value, got {value!r}")
        parsed.append((name, rest))

    return tuple(parsed)

class SocketSession:
    """UNIX-socket-backed live session: bind, serve, dispatch, unlink.

    Subclasses carry their own state dataclass, response type, waybar
    module and `_dispatch`. The socket path is resolved per call rather
    than captured at construction, because the click group callback
    rebinds the module-level paths once `--session` is known."""

    WAYBAR_MODULE: str
    # Response dataclass the handler answers with when `_dispatch` raises.
    RESPONSE: type
    log: logging.Logger
    state: Any

    def __init__(self):
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None

    @staticmethod
    def _socket_path() -> str:
        raise NotImplementedError

    def _dispatch(self, raw: str) -> Any:
        raise NotImplementedError

    def start(self):
        # Session leader → KILL reaches every child subprocess.
        try:
            os.setpgrp()
        except OSError as e:
            self.log.warning("setpgrp failed: %s", e)

        path = self._socket_path()
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(path)
        os.chmod(path, 0o600)
        self._sock.listen(4)
        self.log.debug("listening on %s", path)
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
        # A dropped reply is indistinguishable from "no session listening" to
        # the client, which answers by starting a second session over the live
        # one and rebinding its socket — so every path must answer.
        try:
            response = self._dispatch(_recv_request(conn))
        except Exception as e:
            self.log.warning("socket handler error: %s", e)
            response = self.RESPONSE(ok=False, error=str(e))
        try:
            conn.sendall(json.dumps(asdict(response)).encode())
        except OSError as e:
            self.log.warning("socket reply failed: %s", e)
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def set_phase(self, phase: StrEnum):
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
            os.unlink(self._socket_path())
        except FileNotFoundError:
            pass
        self._signal_waybar()

class SttSession(SocketSession):
    """Live recording session."""

    WAYBAR_MODULE = "stt"
    RESPONSE = Response

    log = logging.getLogger("speech.stt.session")

    def __init__(
        self,
        output: OutputAdapter,
        enricher: EnrichAdapter | None,
        adapter: SttRecorder,
    ):
        super().__init__()
        self.state = SessionState(
            phase=Phase.RECORDING,
            output=output.mode,
            enrich=enricher.provider if enricher else None,
        )
        self.output = output
        self.enricher = enricher
        self._adapter = adapter

    @staticmethod
    def _socket_path() -> str:
        return _STT_PATHS.socket_path

    def set_enricher(self, enricher: EnrichAdapter | None) -> None:
        with self._lock:
            self.enricher = enricher
            self.state.enrich = enricher.provider if enricher else None
        self._signal_waybar()

    def set_output(self, output: OutputAdapter) -> None:
        with self._lock:
            self.output = output
            self.state.output = output.mode
        self._signal_waybar()

    def _dispatch(self, raw: str) -> Response:
        try:
            obj = json.loads(raw) if raw else {}
            cmd = Command(obj.get("cmd", ""))
        except json.JSONDecodeError, ValueError:
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

    def _apply_enrich_override(self, spec_dict: dict | None) -> None:
        new_enricher: EnrichAdapter | None = None
        if spec_dict:
            new_enricher = build_enricher(
                EnrichSpec.from_dict(spec_dict),
                Stt.SYSTEM_PROMPT,
                Stt.USER_PROMPT,
            )
        self.set_enricher(new_enricher)

    def _apply_output_override(self, mode: OutputMode) -> None:
        # A file sink carries a path the socket payload does not, so
        # build_output refuses it and the client is told why.
        self.set_output(build_output(mode))

class Stt:
    ICON = "/usr/share/icons/Adwaita/scalable/devices/microphone.svg"
    SYSTEM_PROMPT = load_prompt("stt.md", relative_to=__file__)
    USER_PROMPT = (
        "Clean up the following speech transcription:\n"
        "<transcription>\n{text}\n</transcription>"
    )

    log = logging.getLogger("speech.stt")

    def __init__(
        self,
        adapter: SttAdapter,
        enricher: EnrichAdapter | None = None,
        output: OutputAdapter | None = None,
    ):
        self._adapter = adapter
        self._enricher = enricher
        self._output = output

    # ── core ──────────────────────────────────────────────────────

    def _notify(self, message, timeout=None):
        notify("Speech-to-Text", message, self.ICON, timeout)

    @classmethod
    def _send(cls, cmd: Command, **kwargs) -> Response | None:
        raw = _rpc(_STT_PATHS.socket_path, cmd.value, **kwargs)
        if raw is None:
            return None
        try:
            return Response.from_json(raw)
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            cls.log.warning("bad response: %s (raw=%r)", e, raw)
            return None

    @property
    def _recorder(self) -> SttRecorder | None:
        """The adapter as a recorder, or None when it drives no microphone.

        A capture that reads a file or a pipe has nothing to toggle: no
        second press to wait for, no socket worth binding, no phase to
        render on the bar."""
        if isinstance(self._adapter, SttRecorder):
            return self._adapter
        return None

    def is_recording(self) -> bool:
        if self._send(Command.STATUS) is not None:
            return True
        recorder = self._recorder
        return recorder.is_recording() if recorder else False

    def run_once(
        self,
        *,
        enrich_spec: EnrichSpec | None,
        output_mode: OutputMode,
        save: bool,
    ) -> None:
        recorder = self._recorder
        if recorder is not None:
            enrich_payload = enrich_spec.to_dict() if enrich_spec else None
            response = self._send(
                Command.STOP, enrich=enrich_payload, output=output_mode.value
            )
            if response is not None:
                self.log.info("press-2: signaled running session to stop")
                # The running session keeps its own output sink when an
                # override is refused, so the transcript lands somewhere
                # other than this invocation asked for.
                if not response.ok:
                    self.log.error("session refused the override: %s", response.error)
                    self._notify(f"Override refused: {response.error}")
                return

        assert self._output is not None, "a capture requires an output adapter"
        self.log.info(
            "capturing via %s output=%s enrich=%s",
            self._adapter.provider.value,
            self._output.mode.value,
            self._enricher.provider.value if self._enricher else None,
        )

        server = (
            SttSession(self._output, self._enricher, recorder) if recorder else None
        )
        if server:
            server.start()
        try:
            try:
                captured = self._adapter.capture()
            except subprocess.CalledProcessError as e:
                reason = e.stderr or f"exit {e.returncode}"
                self.log.error("recorder failed: %s", reason)
                self._notify(f"Recorder failed:\n{reason}")
                sys.exit(1)
            except (
                urllib.error.URLError,
                urllib.error.HTTPError,
                TimeoutError,
                json.JSONDecodeError,
                KeyError,
            ) as e:
                self.log.error("transcription failed: %s", e)
                self._notify("Transcription failed")
                sys.exit(1)

            if captured is None:
                self.log.error("capture produced nothing to transcribe")
                self._notify("Capture failed")
                sys.exit(1)

            # Exit non-zero rather than write an empty sink: a caller that
            # reads the output file cannot tell "silence" from "the model
            # never answered", and the clipboard would be cleared.
            text = captured.strip()
            if not text:
                self.log.warning("empty transcription")
                self._notify("No transcription captured")
                sys.exit(1)

            self.log.info("captured %d chars", len(text))

            # The socket overrides land on the session, so read both back
            # from it rather than from the values this process started with.
            enricher = server.enricher if server else self._enricher
            output = server.output if server else self._output
            if enricher is not None:
                if server:
                    server.set_phase(Phase.WORKING)
                if save and not is_headless():
                    self.log.debug("saving raw transcription to clipboard")
                    OutputAdapterClipboard().write(text)
                self._notify("Enriching transcription...", timeout=3000)
                enriched = enricher.enrich(text)
                if enriched and enriched.strip():
                    text = enriched.strip()
                else:
                    self.log.warning("enrichment empty; using raw")
                    self._notify("Enrichment failed, using raw transcription")

            if server:
                server.set_phase(Phase.OUTPUT)
            output.write(text)
            if enricher is not None:
                self._notify("Done")
        finally:
            if server:
                server.stop()

    def stop(self):
        recorder = self._recorder
        if self._send(Command.STOP) is None and recorder:
            recorder.stop()
            SttSession._signal_waybar()

    def kill(self):
        recorder = self._recorder
        if self._send(Command.KILL) is None and recorder:
            recorder.cancel()
            SttSession._signal_waybar()

    def status_json(self) -> str:
        resp = self._send(Command.STATUS)
        state = resp.state if resp and resp.ok else None

        if state is None:
            recorder = self._recorder
            if recorder and recorder.is_recording():
                return json.dumps(
                    {
                        "class": Phase.RECORDING.value,
                        "text": "󰍬",
                        "tooltip": "Recording (no session)",
                    }
                )
            return json.dumps(
                {"class": "idle", "text": "", "tooltip": "Speech-to-text ready"}
            )

        icons = {
            OutputMode.CLIPBOARD: "󰅇",
            OutputMode.TYPE: "󰌌",
            OutputMode.STDOUT: "󰼭",
            OutputMode.FILE: "󰈔",
        }
        labels = {
            OutputMode.CLIPBOARD: "clipboard",
            OutputMode.TYPE: "typing",
            OutputMode.STDOUT: "stdout",
            OutputMode.FILE: "file",
        }
        icon = icons[state.output]
        label = labels[state.output]
        enrich_label = f" ({state.enrich.value})" if state.enrich else ""

        mapping = {
            Phase.RECORDING: (f"󰍬 {icon}", f"Recording speech → {label}"),
            Phase.WORKING: (f"󰼭 󰧑 {icon}", f"Processing{enrich_label} → {label}"),
            Phase.OUTPUT: (icon, f"Outputting → {label}"),
        }
        text, tooltip = mapping[state.phase]
        return json.dumps(
            {"class": state.phase.value, "text": text, "tooltip": tooltip}
        )

    # ── CLI ───────────────────────────────────────────────────────

    @click.group()
    @click.option(
        "--session",
        "session_suffix",
        default="",
        metavar="SUFFIX",
        help="Socket-path suffix.",
    )
    def cli(session_suffix: str):
        """Control speech-to-text via an STT adapter."""
        global _STT_PATHS
        _STT_PATHS = SttPaths.from_suffix(session_suffix or "")

    @cli.command("toggle")
    @click.option(
        "--source",
        type=click.Choice([p.value for p in SttProvider], case_sensitive=False),
        default=SttProvider.HYPRWHSPR.value,
        help="Transcription backend.",
    )
    @click.option(
        "--input",
        "input_",
        type=click.Choice([m.value for m in InputMode], case_sensitive=False),
        default=InputMode.FILE.value,
        help="Audio source for the http backend.",
    )
    @click.option(
        "--input-file", type=click.Path(path_type=Path), help="Audio file to read."
    )
    @click.option(
        "--output",
        type=click.Choice([m.value for m in OutputMode], case_sensitive=False),
        default=OutputMode.CLIPBOARD.value,
        help="Output sink.",
    )
    @click.option(
        "--output-file", type=click.Path(path_type=Path), help="Text file to write."
    )
    @click.option("--model", default=None, help="Transcription model id.")
    @click.option(
        "--response-format",
        type=click.Choice([f.value for f in ResponseFormat], case_sensitive=False),
        default=ResponseFormat.TEXT.value,
        help="Transcript shape from the backend.",
    )
    @click.option(
        "--language",
        default=DEFAULT_STT_LANGUAGE,
        help="Spoken language hint; empty to auto-detect.",
    )
    @click.option(
        "--field",
        "fields",
        multiple=True,
        metavar="NAME=VALUE",
        help="Extra form field for the backend; repeatable.",
    )
    @click.option(
        "--header",
        "headers",
        multiple=True,
        metavar="NAME=VALUE",
        help="Extra request header for the backend; repeatable.",
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
        default=DEFAULT_STT_TIMEOUT,
        help="Backend deadline in seconds.",
    )
    @click.option("--enrich", is_flag=True, help="Enrich transcription through AI.")
    @enrich_options("enrich")
    @click.option(
        "--save/--no-save", default=True, help="Copy raw transcript to clipboard first."
    )
    def cmd_toggle(
        source,
        input_,
        input_file,
        output,
        output_file,
        model,
        response_format,
        language,
        fields,
        headers,
        base_url,
        api_key_env,
        timeout,
        enrich,
        save,
        **enrich_opts,
    ):
        """Start a session, or toggle an existing one."""
        output_mode = OutputMode(output)
        try:
            output_adapter = build_output(output_mode, path=output_file)
        except (TypeError, ValueError) as e:
            raise click.UsageError(str(e)) from e

        stt_format = ResponseFormat(response_format)
        provider = SttProvider(source)
        adapter: SttAdapter
        match provider:
            case SttProvider.HYPRWHSPR:
                adapter = SttAdapterHyprwhspr()
            case SttProvider.HTTP:
                if not os.environ.get(api_key_env, "").strip():
                    raise click.UsageError(f"{api_key_env} is empty")
                try:
                    audio_source = build_input(InputMode(input_), path=input_file)
                except (TypeError, ValueError) as e:
                    raise click.UsageError(str(e)) from e
                adapter = SttAdapterHttp(
                    SttSpec(
                        model=model,
                        base_url=base_url,
                        api_key_env=api_key_env,
                        response_format=stt_format,
                        language=language,
                        fields=_pairs(fields, "--field"),
                        headers=_pairs(headers, "--header"),
                        timeout=timeout,
                        user_agent="speech/1.0",
                    ),
                    audio_source,
                )
            case _:
                raise click.UsageError(f"unsupported source: {provider!r}")

        # srt and vtt carry timings the cleanup prompt would rewrite away.
        if enrich and stt_format not in PLAIN_FORMATS:
            raise click.UsageError(f"--enrich needs a plain format, not {stt_format}")

        enrich_spec: EnrichSpec | None = None
        enricher: EnrichAdapter | None = None
        if enrich:
            enrich_spec = spec_from_options(enrich_opts, "speech/1.0", "enrich")
            enricher = build_enricher(enrich_spec, Stt.SYSTEM_PROMPT, Stt.USER_PROMPT)

        Stt(adapter, enricher, output_adapter).run_once(
            enrich_spec=enrich_spec,
            output_mode=output_mode,
            save=save,
        )

    @cli.command("enrich")
    @click.option(
        "--input",
        "input_",
        type=click.Choice([m.value for m in InputMode], case_sensitive=False),
        default=InputMode.STDIN.value,
        help="Text source.",
    )
    @click.option(
        "--input-file", type=click.Path(path_type=Path), help="Text file to read."
    )
    @click.option(
        "--output",
        type=click.Choice([m.value for m in OutputMode], case_sensitive=False),
        default=OutputMode.STDOUT.value,
        help="Output sink.",
    )
    @click.option(
        "--output-file", type=click.Path(path_type=Path), help="Text file to write."
    )
    @enrich_options()
    def cmd_enrich(
        input_,
        input_file,
        output,
        output_file,
        **enrich_opts,
    ):
        """Enrich already-transcribed text, without capturing audio.

        No recorder, no session socket, no waybar phase — just the
        cleanup prompt, so a transcript from any other source can reuse
        it."""
        input_mode = InputMode(input_)
        output_mode = OutputMode(output)
        try:
            input_adapter = build_input(input_mode, path=input_file)
            output_adapter = build_output(output_mode, path=output_file)
        except (TypeError, ValueError) as e:
            raise click.UsageError(str(e)) from e

        spec = spec_from_options(enrich_opts, "speech/1.0")
        enricher = build_enricher(spec, Stt.SYSTEM_PROMPT, Stt.USER_PROMPT)

        text = input_adapter.read()
        if not text or not text.strip():
            Stt.log.warning("%s was empty", input_mode.value)
            return

        Stt.log.info("%s text: %d chars", input_mode.value, len(text))
        enriched = enricher.enrich(text)
        if enriched and enriched.strip():
            output_adapter.write(enriched.strip())
        else:
            Stt.log.warning("enrichment empty; emitting raw text")
            output_adapter.write(text.strip())

    @cli.command("stop")
    def cmd_stop():
        """Stop the active session."""
        Stt(SttAdapterHyprwhspr()).stop()

    @cli.command("kill")
    def cmd_kill():
        """Kill the session's process group."""
        Stt(SttAdapterHyprwhspr()).kill()

    @cli.command("status")
    def cmd_status():
        """Print waybar-shaped status JSON."""
        sys.stdout.write(Stt(SttAdapterHyprwhspr()).status_json() + "\n")

    @cli.command("is-recording")
    def cmd_is_recording():
        """Exit 0 if a recording is live."""
        sys.exit(0 if Stt(SttAdapterHyprwhspr()).is_recording() else 1)

class TtsPhase(StrEnum):
    WORKING = "working"
    SPEAKING = "speaking"

@dataclass
class TtsState:
    phase: TtsPhase
    voice: str
    chars: int

@dataclass
class TtsResponse:
    ok: bool
    state: TtsState | None = None
    error: str | None = None

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

class TtsSession(SocketSession):
    """Live playback session."""

    WAYBAR_MODULE = "tts"
    RESPONSE = TtsResponse

    log = logging.getLogger("speech.tts.session")

    def __init__(self, voice: str, chars: int):
        super().__init__()
        self.state = TtsState(phase=TtsPhase.WORKING, voice=voice, chars=chars)

    @staticmethod
    def _socket_path() -> str:
        return _TTS_PATHS.socket_path

    def set_chars(self, chars: int) -> None:
        with self._lock:
            self.state.chars = chars
        self._signal_waybar()

    def _dispatch(self, raw: str) -> TtsResponse:
        try:
            obj = json.loads(raw) if raw else {}
            cmd = Command(obj.get("cmd", ""))
        except json.JSONDecodeError, ValueError:
            return TtsResponse(ok=False, error=f"bad request: {raw!r}")

        self.log.info("socket cmd: %s", cmd.value)
        if cmd is Command.STATUS:
            with self._lock:
                return TtsResponse(ok=True, state=TtsState(**asdict(self.state)))
        if cmd is Command.KILL:
            os.killpg(0, signal.SIGKILL)

        return TtsResponse(ok=False, error=f"unhandled command: {cmd.value}")

def tts_speak_options():
    """Stack the synthesis knobs `speak` and `toggle` share.

    `toggle` forwards its kwargs straight into `speak`, so a knob added
    here reaches both paths and neither can drift from the other."""
    options = [
        click.option(
            "--input",
            "input_",
            type=click.Choice([m.value for m in InputMode], case_sensitive=False),
            default=InputMode.CLIPBOARD.value,
            help="Text source.",
        ),
        click.option(
            "--input-file", type=click.Path(path_type=Path), help="Text file to read."
        ),
        click.option("--voice", default=DEFAULT_TTS_VOICE, help="Backend voice id."),
        click.option("--model", default=None, help="TTS model id."),
        click.option(
            "--speed", type=float, default=1.3, help="Speaking rate multiplier."
        ),
        click.option(
            "--format",
            "response_format",
            type=click.Choice([f.value for f in AudioFormat], case_sensitive=False),
            default=AudioFormat.PCM.value,
            help="Audio encoding.",
        ),
        click.option(
            "--sample-rate",
            type=int,
            default=DEFAULT_TTS_SAMPLE_RATE,
            help="Output rate in Hz.",
        ),
        click.option(
            "--player",
            type=click.Choice([p.value for p in PlayerMode], case_sensitive=False),
            default=DEFAULT_TTS_PLAYER.value,
            help="Playback sink.",
        ),
        click.option(
            "--base-url", default=DEFAULT_BASE_URL, help="HTTP backend base URL."
        ),
        click.option(
            "--api-key-env",
            default=DEFAULT_API_KEY_ENV,
            help="Env var holding the HTTP backend key.",
        ),
        click.option(
            "--timeout",
            type=float,
            default=DEFAULT_TTS_TIMEOUT,
            help="Backend deadline in seconds.",
        ),
        click.option(
            "--copy/--no-copy", default=False, help="Copy the audio to the clipboard."
        ),
        click.option(
            "--enrich/--no-enrich",
            default=False,
            help="Rewrite the text to be readable aloud.",
        ),
    ]

    def decorate(f):
        f = enrich_options("enrich")(f)
        for option in reversed(options):
            f = option(f)

        return f

    return decorate

class Tts:
    ICON = "/usr/share/icons/Adwaita/scalable/devices/audio-headphones.svg"
    # wl-paste also advertises the legacy X11 selection atoms, and some
    # toolkits offer nothing else for plain text.
    TEXT_ATOMS = ("UTF8_STRING", "STRING", "TEXT")
    SYSTEM_PROMPT = load_prompt("tts.md", relative_to=__file__)
    USER_PROMPT = (
        "Rewrite the following text to be read aloud:\n<text>\n{text}\n</text>"
    )

    log = logging.getLogger("speech.tts")

    def __init__(
        self,
        spec: TtsSpec | None = None,
        input: InputAdapter | None = None,
        player: PlayerAdapter | None = None,
        copy: bool = False,
        enricher: EnrichAdapter | None = None,
    ):
        self._spec = spec or TtsSpec()
        self._input = input
        self._player = player
        self._copy = copy
        self._enricher = enricher

    # ── core ──────────────────────────────────────────────────────

    def _notify(self, message, timeout=None):
        notify("Text-to-Speech", message, self.ICON, timeout)

    @classmethod
    def _send(cls, cmd: Command, **kwargs) -> TtsResponse | None:
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

    def _read_text(self) -> str | None:
        """Text to speak, or None with the user already notified why."""
        assert self._input is not None
        if self._input.mode is InputMode.CLIPBOARD:
            types = InputAdapterClipboard.list_mime_types()
            if not any(t.startswith("text/") or t in self.TEXT_ATOMS for t in types):
                self.log.warning("clipboard holds no text: %s", types)
                self._notify("Clipboard holds no text")
                return None

        text = self._input.read()
        if not text or not text.strip():
            self.log.warning("%s was empty", self._input.mode.value)
            self._notify(f"{self._input.mode.value.capitalize()} is empty")
            return None

        return text.strip()

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
            # Enrich before synthesis, not after: the backend reads whatever
            # it is handed, so the rewrite has to land before the audio does.
            if self._enricher is not None:
                self._notify("Rewriting for speech...", timeout=3000)
                rewritten = self._enricher.enrich(text)
                if rewritten and rewritten.strip():
                    text = rewritten.strip()
                    session.set_chars(len(text))
                    self.log.info("rewritten to %d chars", len(text))
                else:
                    self.log.warning("rewrite empty; speaking raw")
                    self._notify("Rewrite failed, speaking raw text")

            buffer = io.BytesIO() if self._copy else None
            # A dead backend and a dead player are different faults with
            # different fixes, so the notification has to tell them apart.
            # Backend first: URLError and TimeoutError are OSError subclasses
            # too, and the player clause below would otherwise swallow them.
            try:
                with TtsAdapterHttp(spec).synth(text) as stream:
                    session.set_phase(TtsPhase.SPEAKING)
                    source = TeeReader(stream, buffer) if buffer else stream
                    written, code = self._player.play(source, spec.sample_rate)
            except (
                urllib.error.URLError,
                urllib.error.HTTPError,
                http.client.IncompleteRead,
                TimeoutError,
            ) as e:
                self.log.error("synthesis failed: %s", e)
                self._notify("Synthesis failed")
                return
            except (FileNotFoundError, BrokenPipeError, OSError) as e:
                self.log.error("playback failed: %s", e)
                self._notify("Playback failed")
                return

            if code != 0:
                self.log.error("%s exit=%d", self._player.mode.value, code)
                self._notify("Playback failed")
                return

            if not written:
                self.log.warning("backend returned no audio")
                self._notify("No audio returned")
                return

            self.log.info(
                "played %d bytes through %s", written, self._player.mode.value
            )
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
            TtsPhase.WORKING: (
                "󰧑 󰕾",
                f"Synthesizing {state.chars} chars ({state.voice})",
            ),
            TtsPhase.SPEAKING: (
                "󰕾",
                f"Speaking {state.chars} chars ({state.voice})",
            ),
        }
        text, tooltip = mapping[state.phase]
        return json.dumps(
            {"class": state.phase.value, "text": text, "tooltip": tooltip}
        )

    # ── CLI ───────────────────────────────────────────────────────

    @click.group()
    @click.option(
        "--session",
        "session_suffix",
        default="",
        metavar="SUFFIX",
        help="Socket-path suffix.",
    )
    def cli(session_suffix: str):
        """Read text aloud through a TTS backend."""
        global _TTS_PATHS
        _TTS_PATHS = TtsPaths.from_suffix(session_suffix or "")

    @cli.command("speak")
    @tts_speak_options()
    def cmd_speak(
        input_,
        input_file,
        voice,
        model,
        speed,
        response_format,
        sample_rate,
        player,
        base_url,
        api_key_env,
        timeout,
        copy,
        enrich,
        **enrich_opts,
    ):
        """Synthesize the input text and play it."""
        try:
            input_adapter = build_input(InputMode(input_), path=input_file)
        except (TypeError, ValueError) as e:
            raise click.UsageError(str(e)) from e

        fmt = AudioFormat(response_format)
        player_mode = PlayerMode(player)
        if fmt is not AudioFormat.PCM and player_mode is not PlayerMode.FFPLAY:
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
            user_agent="speech/1.0",
        )

        enricher: EnrichAdapter | None = None
        if enrich:
            enricher = build_enricher(
                spec_from_options(enrich_opts, "speech/1.0", "enrich"),
                Tts.SYSTEM_PROMPT,
                Tts.USER_PROMPT,
            )

        Tts(spec, input_adapter, player_adapter, copy, enricher).speak()

    @cli.command("toggle")
    @tts_speak_options()
    @click.pass_context
    def cmd_toggle(ctx: click.Context, **speak_opts):
        """Kill an active session, or speak the input."""
        if Tts().is_speaking():
            Tts().kill()
            return

        ctx.invoke(Tts.cmd_speak, **speak_opts)

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
@click.option("--headless", is_flag=True, help="Skip notifications and waybar signals.")
def cli(verbose: bool, headless: bool):
    """Speech-to-text capture and text-to-speech playback."""
    create_logger(verbose)
    set_headless(headless)

cli.add_command(Stt.cli, "stt")
cli.add_command(Tts.cli, "tts")

if __name__ == "__main__":
    cli()
