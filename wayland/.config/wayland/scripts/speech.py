#!/usr/bin/env -S sh -c 'exec uv run --project "$(dirname "$0")" "$0" "$@"'

from __future__ import annotations

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
    DEFAULT_ENRICH_ADAPTER,
    EnrichAdapter,
    EnrichAdapterClaude,
    EnrichAdapterHttp,
    EnrichAdapterOpenCode,
    EnrichProvider,
    OutputAdapter,
    OutputAdapterClipboard,
    OutputAdapterStdout,
    OutputAdapterType,
    OutputMode,
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
class EnrichSpec:
    provider: EnrichProvider
    base_url: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    thinking: str = "none"
    num_ctx: Optional[int] = None

    @classmethod
    def from_dict(cls, d: dict) -> EnrichSpec:
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


# Populated by the root click callback once `--session` is known.
_PATHS: SpeechPaths = SpeechPaths.from_suffix("")


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
        try:
            raw = conn.recv(1024).decode("utf-8", errors="replace").strip()
            response = self._dispatch(raw)
            conn.sendall(json.dumps(asdict(response)).encode())
        except Exception as e:
            self.log.warning("socket handler error: %s", e)
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
            if "enrich" in obj:
                self._apply_enrich_override(obj["enrich"])
            if obj.get("output"):
                self._apply_output_override(OutputMode(obj["output"]))
            self._adapter.stop()
            return Response(ok=True)
        if cmd is Command.KILL:
            self._adapter.cancel()
            os.killpg(0, signal.SIGKILL)

        return Response(ok=False, error=f"unhandled command: {cmd.value}")

    def _apply_enrich_override(self, spec_dict: Optional[dict]) -> None:
        new_enricher: Optional[EnrichAdapter] = None
        if spec_dict:
            spec = EnrichSpec.from_dict(spec_dict)
            model_kw = {"model": spec.model} if spec.model else {}
            match spec.provider:
                case EnrichProvider.HTTP:
                    new_enricher = EnrichAdapterHttp(
                        Speech.SYSTEM_PROMPT,
                        Speech.USER_PROMPT,
                        base_url=spec.base_url or "https://ai.kilic.dev/api/v1",
                        api_key=spec.api_key or "",
                        temperature=spec.temperature,
                        top_p=spec.top_p,
                        thinking=spec.thinking,
                        num_ctx=spec.num_ctx,
                        user_agent="speech/1.0",
                        **model_kw,
                    )
                case EnrichProvider.CLAUDE:
                    new_enricher = EnrichAdapterClaude(
                        Speech.SYSTEM_PROMPT, Speech.USER_PROMPT, **model_kw
                    )
                case EnrichProvider.OPENCODE:
                    new_enricher = EnrichAdapterOpenCode(
                        Speech.SYSTEM_PROMPT, Speech.USER_PROMPT, **model_kw
                    )
                case _:
                    raise ValueError(f"unknown enrich provider: {spec.provider!r}")
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
            cls.log.warning("socket connect failed: %s", e)
            return None

        try:
            payload = json.dumps({"cmd": cmd.value, **kwargs}) + "\n"
            cls.log.debug("rpc send: %s", payload.rstrip())
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
                cls.log.warning("bad response: %s (raw=%r)", e, raw)
                return None
        finally:
            sock.close()

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
        enrich_payload = asdict(enrich_spec) if enrich_spec else None
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

    @click.group(context_settings={"help_option_names": ["-h", "--help"]})
    @click.option("-v", "--verbose", is_flag=True, help="Enable debug logging.")
    @click.option("--session", "session_suffix", default="", metavar="SUFFIX", help="Socket-path suffix.")
    def cli(verbose: bool, session_suffix: str):
        """Control speech-to-text via an STT adapter."""
        create_logger(verbose)
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
    @click.option("--enrich-base-url", default="https://ai.kilic.dev/api/v1")
    @click.option("--enrich-model", default=None)
    @click.option("--enrich-temperature", type=float, default=None)
    @click.option("--enrich-top-p", type=float, default=None)
    @click.option(
        "--enrich-thinking",
        type=click.Choice(["high", "medium", "low", "none"]),
        default="none",
    )
    @click.option("--enrich-num-ctx", type=int, default=None)
    @click.option("--save/--no-save", default=True, help="Copy raw transcript to clipboard first.")
    def cmd_toggle(
        output,
        enrich,
        enrich_provider,
        enrich_base_url,
        enrich_model,
        enrich_temperature,
        enrich_top_p,
        enrich_thinking,
        enrich_num_ctx,
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
            provider_enum = EnrichProvider(enrich_provider)
            enrich_spec = EnrichSpec(
                provider=provider_enum,
                base_url=enrich_base_url,
                model=enrich_model,
                api_key=os.environ.get("AI_KILIC_DEV_API_KEY", ""),
                temperature=enrich_temperature,
                top_p=enrich_top_p,
                thinking=enrich_thinking,
                num_ctx=enrich_num_ctx,
            )
            model_kw = {"model": enrich_model} if enrich_model else {}
            match provider_enum:
                case EnrichProvider.HTTP:
                    enricher = EnrichAdapterHttp(
                        Speech.SYSTEM_PROMPT,
                        Speech.USER_PROMPT,
                        base_url=enrich_base_url,
                        api_key=os.environ.get("AI_KILIC_DEV_API_KEY", ""),
                        temperature=enrich_temperature,
                        top_p=enrich_top_p,
                        thinking=enrich_thinking,
                        num_ctx=enrich_num_ctx,
                        user_agent="speech/1.0",
                        **model_kw,
                    )
                case EnrichProvider.CLAUDE:
                    enricher = EnrichAdapterClaude(
                        Speech.SYSTEM_PROMPT, Speech.USER_PROMPT, **model_kw
                    )
                case EnrichProvider.OPENCODE:
                    enricher = EnrichAdapterOpenCode(
                        Speech.SYSTEM_PROMPT, Speech.USER_PROMPT, **model_kw
                    )
                case _:
                    raise click.UsageError(f"unknown enrich provider: {provider_enum!r}")

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


if __name__ == "__main__":
    Speech.cli()
