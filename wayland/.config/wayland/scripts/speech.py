#!/usr/bin/env -S sh -c 'exec uv run --project "$(dirname "$0")" "$0" "$@"'

from __future__ import annotations

import http.client
import io
import json
import logging
import math
import os
import signal
import socket
import sys
import threading
import urllib.error
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import click
from dotlib.audio import (
    DEFAULT_DUCK_FACTOR,
    PlaybackSuppressor,
)
from dotlib.cli import (
    create_logger,
)
from dotlib.desktop import (
    is_headless,
    set_headless,
)
from dotlib.notify import (
    Chime,
    ChimeDirection,
    Notification,
    OsdIcon,
)

from lib import (
    DEFAULT_API_KEY_ENV,
    DEFAULT_BASE_URL,
    DEFAULT_STT_LANGUAGE,
    DEFAULT_STT_TIMEOUT,
    DEFAULT_VAD_SILENCE_MS,
    DEFAULT_VAD_THRESHOLD,
    DEFAULT_TTS_PLAYER,
    DEFAULT_TTS_SAMPLE_RATE,
    DEFAULT_TTS_TIMEOUT,
    DEFAULT_TTS_VOICE,
    PLAIN_FORMATS,
    RealtimeUnavailable,
    AudioFormat,
    EnrichAdapter,
    EnrichProvider,
    EnrichSpec,
    EnrichStreaming,
    InputAdapter,
    InputAdapterClipboard,
    InputMode,
    LevelReader,
    LevelSource,
    OutputAdapter,
    OutputAdapterClipboard,
    OutputMode,
    OutputStreaming,
    PlayerAdapter,
    PlayerAdapterFfplay,
    PlayerAdapterPaplay,
    PlayerAdapterPwCat,
    PlayerMode,
    PrefixReader,
    ResponseFormat,
    SttAdapter,
    SttAdapterHttp,
    SttAdapterMic,
    SttAdapterRealtime,
    SttProvider,
    SttRecorder,
    SttSpec,
    SttStreaming,
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
    ENQUEUE = "enqueue"

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

SUBSCRIPT_DIGITS = str.maketrans("0123456789", "₀₁₂₃₄₅₆₇₈₉")

def subscript(value: int) -> str:
    """Render a number as subscript digits, for hanging a count off an icon."""
    return str(value).translate(SUBSCRIPT_DIGITS)

def _meter(peak: float) -> float:
    """A 0-to-1 bar position for a peak sample level.

    Decibels, because hearing is logarithmic and a linear peak leaves normal
    speech sitting in the bottom tenth of the bar. -60dB is the bottom of the
    scale: quieter than that is a silent room, not a quiet talker.

    Never quite empty. A bar at zero is indistinguishable from no bar, and the
    point of the meter is to show that something is live at all."""
    decibels = 20 * math.log10(peak) if peak > 0 else -120.0
    scaled = (decibels + 60.0) / 60.0

    return min(1.0, max(0.04, scaled))

def _level(adapter: SttAdapter) -> float | None:
    """How loud the microphone is right now, 0 to 1, or None if unknowable.

    A single number rather than a row of bars: the card is one line of text on
    a surface shared with the volume popup, and a column chart drawn out of
    block characters reads as mojibake at that size. An adapter driving
    someone else's daemon never sees the samples and answers None."""
    if not isinstance(adapter, LevelSource):
        return None
    frame = adapter.frame()
    if frame is None:
        return None

    peak, _ = frame

    return _meter(peak)

def _input_choices(*modes: InputMode) -> click.Choice:
    """Choice over exactly the input modes a command can build.

    Spelled out per command rather than taken from the enum: `InputMode` also
    carries modes no flag can select — a live capture belongs to the adapter
    that drives it, and `build_input` would refuse it."""
    return click.Choice([m.value for m in modes], case_sensitive=False)

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
        suppressor: PlaybackSuppressor,
    ):
        super().__init__()
        self.state = SessionState(
            phase=Phase.RECORDING,
            output=output.mode,
            enrich=enricher.provider if enricher else None,
        )
        self.output = output
        self.enricher = enricher
        self.suppressor = suppressor
        self._adapter = adapter
        # Set once turns start reaching the sink. What is typed cannot be
        # taken back, so an override that would rewrite the take arrives too
        # late to be honoured and is refused rather than dropped.
        self.emitting = False

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

        # A status poll is the bar asking on its interval; only a command
        # that changes something is worth a line.
        self.log.log(
            logging.DEBUG if cmd is Command.STATUS else logging.INFO,
            "socket cmd: %s",
            cmd.value,
        )
        if cmd is Command.STATUS:
            with self._lock:
                return Response(ok=True, state=SessionState(**asdict(self.state)))
        if cmd is Command.STOP:
            # Stop first: an override that raises must not leave the recorder
            # running with no second toggle able to reach it.
            self._adapter.stop()
            # The microphone is shut, so nothing more can bleed into it.
            # Transcription and enrichment still have seconds to run, and
            # holding everyone else quiet through an LLM call is not something
            # the recording needed - `run_once` keeps a restore of its own for
            # the paths that never reach a STOP.
            self.suppressor.restore()
            # The job is not done though: nothing is written until the
            # transcription and any enrichment come back.
            Chime(ChimeDirection.FLAT).play()
            if "enrich" in obj:
                if self.emitting and obj["enrich"]:
                    return Response(
                        ok=False,
                        error="turns already typed; a rewrite cannot replace them",
                    )
                self._apply_enrich_override(obj["enrich"])
            if obj.get("output"):
                self._apply_output_override(OutputMode(obj["output"]))
            return Response(ok=True)
        if cmd is Command.KILL:
            self._adapter.cancel()
            # SIGKILL runs no `finally`, so the ducked streams and the paused
            # players have to be put back before it lands.
            self.suppressor.restore()
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
    NOTIFICATION = Notification("STT", ICON, OsdIcon.MIC)
    SYSTEM_PROMPT = load_prompt("stt.md", relative_to=__file__)
    # Biases the recogniser toward the vocabulary this machine dictates in.
    WHISPER_PROMPT = load_prompt("stt-whisper.md", relative_to=__file__)
    USER_PROMPT = (
        "Clean up the following speech transcription:\n"
        "<transcription>\n{text}\n</transcription>"
    )

    log = logging.getLogger("speech.stt")

    def __init__(
        self,
        adapter: SttAdapter | None = None,
        enricher: EnrichAdapter | None = None,
        output: OutputAdapter | None = None,
        duck: bool = False,
        duck_factor: float = DEFAULT_DUCK_FACTOR,
        pause_players: bool = False,
    ):
        self._adapter = adapter
        self._enricher = enricher
        self._output = output
        self._suppressor = PlaybackSuppressor(
            duck=duck,
            factor=duck_factor,
            pause=pause_players,
            name="speech",
        )

    # ── core ──────────────────────────────────────────────────────

    def _notify(self, message, timeout=None):
        self.NOTIFICATION.send(message, timeout)

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
        stream: bool = False,
    ) -> None:
        assert self._adapter is not None, "a capture needs an adapter"
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
                    self._notify(f"Override refused: {response.error}", timeout=6000)
                return

        assert self._output is not None, "a capture requires an output adapter"
        self.log.info(
            "capturing via %s output=%s enrich=%s",
            self._adapter.provider.value,
            self._output.mode.value,
            self._enricher.provider.value if self._enricher else None,
        )

        server = (
            SttSession(self._output, self._enricher, recorder, self._suppressor)
            if recorder
            else None
        )
        if server:
            server.start()
        # The only cue that the microphone is live. Before the suppression, so
        # it is heard at full volume rather than ducking itself.
        Chime(ChimeDirection.UP).play()

        osd = self.NOTIFICATION
        # Finalised turns only — the endpoint publishes no partial deltas, so
        # what reaches the card is never taken back.
        transcript: list[str] = []

        # The meter ticks on one thread while turns close on another, and
        # swayosd draws only what a call carries, so both redraw the whole
        # card from what this run holds.
        def redraw() -> None:
            osd.elapsed(
                osd.tail(" ".join(transcript)), level=_level(self._adapter), apart=True
            )

        # Turns go to the sink as they close, not just to the card, when the
        # sink can take them and nothing downstream will rewrite them. An
        # enrichment would, so it rules this out. Read off this invocation
        # rather than the session: a socket override lands after the capture,
        # and by then the early turns are already out.
        live_sink = self._output
        live = (
            stream
            and self._enricher is None
            and isinstance(live_sink, OutputStreaming)
            and isinstance(self._adapter, SttStreaming)
        )
        redraw()
        if isinstance(self._adapter, SttStreaming):

            def on_turn(text: str) -> None:
                transcript.append(text)
                redraw()
                if live:
                    if server:
                        server.emitting = True
                    live_sink.write(text if len(transcript) == 1 else f" {text}")

            self._adapter.subscribe(on_turn)
        # swayosd hides a card when its own timer runs out, so holding one open
        # for an open-ended recording means re-firing. A daemon thread rather
        # than the capture loop, which is blocked inside the adapter.
        ticking = threading.Event()

        def tick() -> None:
            while not ticking.wait(1.0):
                redraw()

        ticker = threading.Thread(target=tick, daemon=True)
        ticker.start()
        # Quieting playback matters more here than for speech: whatever the
        # speakers are doing bleeds into the microphone and lands in the
        # transcript. Lifted again by the STOP handler as soon as the
        # microphone shuts.
        self._suppressor.suppress()
        try:
            try:
                captured = self._adapter.capture()
            except RealtimeUnavailable as e:
                self.log.error("realtime failed: %s", e)
                self._notify(f"Realtime failed: {e}", timeout=8000)
                sys.exit(1)
            except (
                urllib.error.URLError,
                urllib.error.HTTPError,
                TimeoutError,
                json.JSONDecodeError,
                KeyError,
            ) as e:
                self.log.error("transcription failed: %s", e)
                self._notify("Transcription failed", timeout=6000)
                sys.exit(1)

            if captured is None:
                self.log.error("capture produced nothing to transcribe")
                self._notify("Capture failed", timeout=6000)
                sys.exit(1)

            # Exit non-zero rather than write an empty sink: a caller that
            # reads the output file cannot tell "silence" from "the model
            # never answered", and the clipboard would be cleared.
            text = captured.strip()
            if not text:
                self.log.warning("empty transcription")
                self._notify("No transcription captured", timeout=6000)
                sys.exit(1)

            self.log.info("captured %d chars", len(text))

            # The socket overrides land on the session, so read both back
            # from it rather than from the values this process started with.
            enricher = server.enricher if server else self._enricher
            output = server.output if server else self._output
            # Regardless of enrichment: by here the microphone is shut and the
            # transcription is back, and leaving the bar on "recording" through
            # a REST round-trip contradicts the chime that already said the mic
            # closed.
            if server:
                server.set_phase(Phase.WORKING)
            ticking.set()
            ticker.join(timeout=Notification.TIMEOUT)
            # When both halves can stream, the sink starts with the first token
            # instead of after the last one. Typing is the slow part — roughly
            # fifty characters a second — so there it overlaps generation with
            # the keystrokes; on stdout it lets whatever is downstream start.
            if (
                stream
                and enricher is not None
                and isinstance(enricher, EnrichStreaming)
                and isinstance(output, OutputStreaming)
            ):
                osd.send(osd.tail(text), icon=OsdIcon.THINKING)
                if save and not is_headless():
                    OutputAdapterClipboard().write(text)
                if server:
                    server.set_phase(Phase.OUTPUT)
                # Show the rewrite arriving rather than a spinner word: the
                # card is the only place the text is visible before it lands
                # in whatever window has focus.
                output.write_stream(osd.echo(enricher.enrich_stream(text), icon=OsdIcon.THINKING))
                osd.dismiss(f"{output.mode.value} done", icon=OsdIcon.DONE)
                Chime(ChimeDirection.DOWN).play()
                return

            if enricher is not None:
                osd.send("enriching", icon=OsdIcon.THINKING)
                if save and not is_headless():
                    self.log.debug("saving raw transcription to clipboard")
                    OutputAdapterClipboard().write(text)
                if isinstance(enricher, EnrichStreaming):
                    # The card can stream even where the sink cannot: a
                    # clipboard holds one value, but watching the rewrite
                    # arrive is what says the wait is going somewhere.
                    enriched = "".join(
                        osd.echo(enricher.enrich_stream(text), icon=OsdIcon.THINKING)
                    )
                else:
                    enriched = enricher.enrich(text)
                if enriched and enriched.strip():
                    text = enriched.strip()
                else:
                    self.log.warning("enrichment empty; using raw")
                    self._notify("Enrichment failed, using raw transcription", timeout=6000)

            if server:
                server.set_phase(Phase.OUTPUT)
            # Whatever the take ended up being, minus what already went out a
            # turn at a time. Only a clean prefix is trusted: anything else
            # means the socket's turns and the finished text disagree, and
            # writing the difference would duplicate or reorder a dictation
            # that is already in the user's window.
            emitted = " ".join(transcript)
            if not live or output is not live_sink:
                # A mid-take override asked for a different sink, so that one
                # has had nothing yet and takes the whole take.
                output.write(text)
            elif text.startswith(emitted):
                if rest := text[len(emitted) :]:
                    output.write(rest)
            else:
                self.log.error(
                    "typed turns do not prefix the take; %d chars dropped", len(text)
                )
                self._notify("Take and typed turns disagree", timeout=8000)
            osd.dismiss(f"{len(text)} chars", icon=OsdIcon.DONE)
            # After the write, not before: the chime means "the text has
            # landed", and a caller typing into a focused window wants the
            # keystrokes to arrive before the sound that announces them.
            Chime(ChimeDirection.DOWN).play()
        finally:
            ticking.set()
            ticker.join(timeout=Notification.TIMEOUT)
            self._suppressor.restore()
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
        default=SttProvider.REALTIME.value,
        help="Transcription backend.",
    )
    @click.option(
        "--vad-threshold",
        type=float,
        default=DEFAULT_VAD_THRESHOLD,
        help="Speech probability a realtime turn needs, not a level.",
    )
    @click.option(
        "--vad-silence-ms",
        type=int,
        default=DEFAULT_VAD_SILENCE_MS,
        help="Silence that closes a realtime turn.",
    )
    @click.option(
        "--input",
        "input_",
        type=_input_choices(
            InputMode.CLIPBOARD, InputMode.STDIN, InputMode.FILE
        ),
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
        "--whisper-prompt",
        default=None,
        help="Vocabulary hint for the recogniser. Empty string disables it.",
    )
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
    @click.option(
        "--stream/--no-stream",
        default=True,
        help=(
            "Emit as it is produced rather than at the end: the rewrite with "
            "--enrich, the realtime turns without it. Needs a type or stdout "
            "output."
        ),
    )
    @click.option(
        "--duck/--no-duck",
        default=True,
        help="Lower other applications while recording.",
    )
    @click.option(
        "--duck-factor",
        type=float,
        default=DEFAULT_DUCK_FACTOR,
        help="Multiplier applied to other applications' volume.",
    )
    @click.option(
        "--pause-players/--no-pause-players",
        default=True,
        help="Pause MPRIS players while recording.",
    )
    def cmd_toggle(
        source,
        vad_threshold,
        vad_silence_ms,
        input_,
        input_file,
        output,
        output_file,
        model,
        whisper_prompt,
        response_format,
        language,
        fields,
        headers,
        base_url,
        api_key_env,
        timeout,
        enrich,
        save,
        stream,
        duck,
        duck_factor,
        pause_players,
        **enrich_opts,
    ):
        """Start a session, or toggle an existing one."""
        output_mode = OutputMode(output)
        try:
            output_adapter = build_output(output_mode, path=output_file)
        except (TypeError, ValueError) as e:
            raise click.UsageError(str(e)) from e

        if whisper_prompt is None:
            whisper_prompt = Stt.WHISPER_PROMPT
        stt_format = ResponseFormat(response_format)
        provider = SttProvider(source)
        adapter: SttAdapter
        match provider:
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
                        prompt=whisper_prompt,
                        fields=_pairs(fields, "--field"),
                        headers=_pairs(headers, "--header"),
                        timeout=timeout,
                        user_agent="speech/1.0",
                    ),
                    audio_source,
                )
            case SttProvider.MIC | SttProvider.REALTIME:
                if not os.environ.get(api_key_env, "").strip():
                    raise click.UsageError(f"{api_key_env} is empty")
                build = (
                    SttAdapterRealtime
                    if provider is SttProvider.REALTIME
                    else SttAdapterMic
                )
                adapter = build(
                    SttSpec(
                        model=model,
                        base_url=base_url,
                        api_key_env=api_key_env,
                        response_format=stt_format,
                        language=language,
                        prompt=whisper_prompt,
                        fields=_pairs(fields, "--field"),
                        headers=_pairs(headers, "--header"),
                        timeout=timeout,
                        user_agent="speech/1.0",
                        vad_threshold=vad_threshold,
                        vad_silence_ms=vad_silence_ms,
                    )
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

        Stt(
            adapter,
            enricher,
            output_adapter,
            duck,
            duck_factor,
            pause_players,
        ).run_once(
            enrich_spec=enrich_spec,
            output_mode=output_mode,
            save=save,
            stream=stream,
        )

    @cli.command("enrich")
    @click.option(
        "--input",
        "input_",
        type=_input_choices(
            InputMode.CLIPBOARD, InputMode.STDIN, InputMode.FILE
        ),
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
        if isinstance(enricher, EnrichStreaming) and isinstance(
            output_adapter, OutputStreaming
        ):
            output_adapter.write_stream(enricher.enrich_stream(text))
            return

        enriched = enricher.enrich(text)
        if enriched and enriched.strip():
            output_adapter.write(enriched.strip())
        else:
            Stt.log.warning("enrichment empty; emitting raw text")
            output_adapter.write(text.strip())

    @cli.command("stop")
    def cmd_stop():
        """Stop the active session."""
        Stt().stop()

    @cli.command("kill")
    def cmd_kill():
        """Kill the session's process group."""
        Stt().kill()

    @cli.command("status")
    def cmd_status():
        """Print waybar-shaped status JSON."""
        sys.stdout.write(Stt().status_json() + "\n")

    @cli.command("is-recording")
    def cmd_is_recording():
        """Exit 0 if a recording is live."""
        sys.exit(0 if Stt().is_recording() else 1)

class TtsPhase(StrEnum):
    WORKING = "working"
    SPEAKING = "speaking"

class TtsStyle(StrEnum):
    READ = "read"
    SUMMARY = "summary"

TTS_PROMPTS = {TtsStyle.READ: "tts.md", TtsStyle.SUMMARY: "tts-summary.md"}

@dataclass
class TtsState:
    phase: TtsPhase
    voice: str
    chars: int
    # Previews of what is waiting behind the current utterance, oldest first.
    queued: list[str] = field(default_factory=list)

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
                queued=list(sd.get("queued") or []),
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

    def __init__(
        self,
        voice: str,
        chars: int,
        suppressor: PlaybackSuppressor,
    ):
        super().__init__()
        self.state = TtsState(phase=TtsPhase.WORKING, voice=voice, chars=chars)
        self.suppressor = suppressor
        self._queue: list[str] = []

    PREVIEW_CHARS = 42

    @classmethod
    def _preview(cls, text: str) -> str:
        """One line of an utterance, short enough for a tooltip row."""
        flat = " ".join(text.split())
        if len(flat) <= cls.PREVIEW_CHARS:
            return flat

        return flat[: cls.PREVIEW_CHARS - 1].rstrip() + "…"

    def enqueue(self, text: str) -> None:
        """Add an utterance to the backlog.

        Unbounded on purpose: a backlog nobody wants is one `tts toggle` away
        from being gone, so dropping an utterance to enforce a cap would throw
        away the one thing the caller cannot get back."""
        with self._lock:
            self._queue.append(text)
        # The bar polls on a 3s interval, which is long enough to miss a short
        # utterance queueing and draining between ticks.
        self._signal_waybar()

    @property
    def phase(self) -> TtsPhase:
        with self._lock:
            return self.state.phase

    def card(self) -> str:
        with self._lock:
            chars = self.state.chars

        return f"{chars} chars{self.waiting()}"

    def waiting(self) -> str:
        """`, 2 waiting` when something is queued, empty when nothing is."""
        with self._lock:
            count = len(self._queue)

        return f", {count} waiting" if count else ""

    def pop(self) -> str | None:
        """Next queued utterance, or None once the backlog is drained."""
        with self._lock:
            text = self._queue.pop(0) if self._queue else None
        if text is not None:
            self._signal_waybar()

        return text

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

        # A status poll is the bar asking on its interval; only a command
        # that changes something is worth a line.
        self.log.log(
            logging.DEBUG if cmd is Command.STATUS else logging.INFO,
            "socket cmd: %s",
            cmd.value,
        )
        if cmd is Command.STATUS:
            with self._lock:
                state = TtsState(**asdict(self.state))
                # Derived here rather than mirrored into the state on every
                # enqueue, so there is one queue and nothing to drift from it.
                state.queued = [self._preview(text) for text in self._queue]
                return TtsResponse(ok=True, state=state)
        if cmd is Command.ENQUEUE:
            # Text only: a queued utterance is spoken by the running session,
            # through the player it already spawned, so a per-item sample rate
            # or format would mean tearing that player down and rebuilding it.
            text = str(obj.get("text") or "").strip()
            if not text:
                return TtsResponse(ok=False, error="enqueue needs text")
            self.enqueue(text)
            with self._lock:
                return TtsResponse(ok=True, state=TtsState(**asdict(self.state)))
        if cmd is Command.KILL:
            # SIGKILL runs no `finally`, so the ducked streams and the paused
            # players have to be put back before it lands, or they stay quiet
            # until their app restarts.
            self.suppressor.restore()
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
            type=_input_choices(
            InputMode.CLIPBOARD, InputMode.STDIN, InputMode.FILE
        ),
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
            "--normalize/--no-normalize",
            default=True,
            help="Lift playback loudness. Needs the ffplay sink.",
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
            "--duck/--no-duck",
            default=True,
            help="Lower other applications while speaking.",
        ),
        click.option(
            "--duck-factor",
            type=float,
            default=DEFAULT_DUCK_FACTOR,
            help="Multiplier applied to other applications' volume.",
        ),
        click.option(
            "--pause-players/--no-pause-players",
            default=True,
            help="Pause MPRIS players while speaking.",
        ),
        click.option(
            "--copy/--no-copy", default=False, help="Copy the audio to the clipboard."
        ),
        click.option(
            "--enrich/--no-enrich",
            default=False,
            help="Rewrite the text to be readable aloud.",
        ),
        click.option(
            "--style",
            type=click.Choice([s.value for s in TtsStyle], case_sensitive=False),
            default=TtsStyle.READ.value,
            help="Read the text in full, or summarize it. Needs --enrich.",
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
    NOTIFICATION = Notification("TTS", ICON, OsdIcon.SPEAKER)
    # wl-paste also advertises the legacy X11 selection atoms, and some
    # toolkits offer nothing else for plain text.
    TEXT_ATOMS = ("UTF8_STRING", "STRING", "TEXT")
    USER_PROMPT = (
        "Rewrite the following text to be read aloud:\n<text>\n{text}\n</text>"
    )

    @staticmethod
    def system_prompt(style: TtsStyle) -> str:
        return load_prompt(TTS_PROMPTS[style], relative_to=__file__)

    log = logging.getLogger("speech.tts")

    def __init__(
        self,
        spec: TtsSpec | None = None,
        input: InputAdapter | None = None,
        player: PlayerAdapter | None = None,
        copy: bool = False,
        enricher: EnrichAdapter | None = None,
        duck: bool = False,
        duck_factor: float = DEFAULT_DUCK_FACTOR,
        pause_players: bool = False,
    ):
        self._spec = spec or TtsSpec()
        self._input = input
        self._player = player
        self._copy = copy
        self._enricher = enricher
        self._meter: LevelReader | None = None
        self._suppressor = PlaybackSuppressor(
            duck=duck,
            factor=duck_factor,
            pause=pause_players,
            name="speech",
            exclude=(player.mode.value,) if player else (),
        )

    # ── core ──────────────────────────────────────────────────────

    def _notify(self, message, timeout=None):
        self.NOTIFICATION.send(message, timeout)

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

    def _enrich(self, text: str, session: TtsSession | None = None) -> str:
        """Rewrite for speech, falling back to the original on any failure."""
        if self._enricher is None:
            return text

        if isinstance(self._enricher, EnrichStreaming):
            # Synthesis needs the whole rewrite before it can speak a word, so
            # the card is the only place the wait shows as progress.
            card = self.NOTIFICATION
            parts: list[str] = []
            for chunk in self._enricher.enrich_stream(text):
                parts.append(chunk)
                card.elapsed(card.tail("".join(parts)), icon=OsdIcon.THINKING, apart=True)
            rewritten = "".join(parts)
        else:
            self._notify("Rewriting for speech...", timeout=3000)
            rewritten = self._enricher.enrich(text)
        if not (rewritten and rewritten.strip()):
            self.log.warning("rewrite empty; speaking raw")
            self._notify("Rewrite failed, speaking raw text")
            return text

        text = rewritten.strip()
        if session is not None:
            session.set_chars(len(text))
        self.log.info("rewritten to %d chars", len(text))

        return text

    def _play(self, text: str, session: TtsSession) -> bool:
        """Synthesize and play one utterance; False once something failed.

        A failure ends the whole run rather than moving to the next queued
        item: a dead backend or a dead player will not have recovered by the
        time the next one is dequeued, and a queue of failures is a queue of
        notifications."""
        spec = self._spec
        buffer = io.BytesIO() if self._copy else None
        self._meter = None
        # A dead backend and a dead player are different faults with
        # different fixes, so the notification has to tell them apart.
        # Backend first: URLError and TimeoutError are OSError subclasses
        # too, and the player clause below would otherwise swallow them.
        try:
            with TtsAdapterHttp(spec).synth(text) as stream:
                session.set_phase(TtsPhase.SPEAKING)
                # The clock measures speech, so it starts here rather than at
                # the top of a run that spends its first seconds rewriting.
                self.NOTIFICATION.restart()
                # Quiet the room at playback, not at synthesis: the backend
                # can take seconds, and pausing before there is anything to
                # hear just makes the wait silent.
                self._suppressor.suppress()
                source = TeeReader(stream, buffer) if buffer else stream
                # Outside the tee, so the chime is heard but never copied. Raw
                # samples can only be prepended to raw samples, so a container
                # format goes without one.
                if spec.response_format is AudioFormat.PCM:
                    source = PrefixReader(
                        Chime(ChimeDirection.UP, spec.sample_rate).pcm(), source
                    )
                    # Outermost, so the chime registers on the meter and the
                    # bar moves from the first sound rather than the first word.
                    source = self._meter = LevelReader(source)
                try:
                    written, code = self._player.play(source, spec.sample_rate)
                finally:
                    # Per utterance, not per run: a queue drain synthesises the
                    # next item before playing it, and holding the room quiet
                    # across that gap quiets it for nothing.
                    self._suppressor.restore()
        except (
            urllib.error.URLError,
            urllib.error.HTTPError,
            http.client.IncompleteRead,
            TimeoutError,
        ) as e:
            self.log.error("synthesis failed: %s", e)
            self._notify("Synthesis failed")
            return False
        except (FileNotFoundError, BrokenPipeError, OSError) as e:
            self.log.error("playback failed: %s", e)
            self._notify("Playback failed")
            return False

        if code != 0:
            self.log.error("%s exit=%d", self._player.mode.value, code)
            self._notify("Playback failed")
            return False

        if not written:
            self.log.warning("backend returned no audio")
            self._notify("No audio returned")
            return False

        self.log.info("played %d bytes through %s", written, self._player.mode.value)
        if buffer is not None:
            copy_audio(buffer.getvalue(), spec)
            self._notify("Audio copied to clipboard", timeout=3000)

        return True

    def speak(self) -> None:
        assert self._input is not None, "speak requires an input adapter"
        assert self._player is not None, "speak requires a player adapter"
        spec = self._spec

        text = self._read_text()
        if text is None:
            return

        # Hand off rather than rebind: a second bind would drop the running
        # session's socket and leave its player unreachable to `kill`. The
        # rewrite happens here, on this side of the socket, because only this
        # invocation knows which enricher and style its flags asked for.
        if self.is_speaking():
            text = self._enrich(text)
            resp = self._send(Command.ENQUEUE, text=text)
            if resp is not None and resp.ok:
                self.log.info("queued %d chars behind the live session", len(text))
                # Deliberately over the speech that is already playing: the
                # point is to say another one has arrived without waiting for
                # the current utterance to end.
                Chime(ChimeDirection.FLAT).play()
                self._notify("Queued behind the current utterance", timeout=3000)
            else:
                error = resp.error if resp else "no session answered"
                self.log.warning("enqueue refused: %s", error)
                self._notify(f"Not queued: {error}")
            return

        self.log.info("speaking %d chars (voice=%s)", len(text), spec.voice)
        osd = self.NOTIFICATION
        session = TtsSession(spec.voice, len(text), self._suppressor)
        session.start()
        # The card ticks from its own thread because the run blocks inside
        # synthesis and playback for as long as an utterance lasts.
        ticking = threading.Event()

        def tick() -> None:
            while not ticking.wait(1.0):
                if session.phase is not TtsPhase.SPEAKING:
                    osd.send(session.card(), osd.TICK_HOLD_MS, icon=OsdIcon.THINKING)
                    continue
                meter = self._meter
                osd.elapsed(
                    session.card(), level=_meter(meter.peak) if meter else None
                )

        ticker = threading.Thread(target=tick, daemon=True)
        ticker.start()
        try:
            # Enrich before synthesis, not after: the backend reads whatever
            # it is handed, so the rewrite has to land before the audio does.
            spoken = self._enrich(text, session)
            while True:
                if not self._play(spoken, session):
                    break
                queued = session.pop()
                if queued is None:
                    osd.dismiss()
                    break
                session.set_phase(TtsPhase.WORKING)
                session.set_chars(len(queued))
                self.log.info("dequeued %d chars", len(queued))
                spoken = queued
        finally:
            ticking.set()
            ticker.join(timeout=Notification.TIMEOUT)
            osd.dismiss()
            self._suppressor.restore()
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
        if state.queued:
            # Subscript rather than a plain digit: the backlog is a footnote to
            # what is playing, and it has to sit against the icon without
            # widening the module every time something queues.
            text += subscript(len(state.queued))
            waiting = "\n".join(f"  {i}. {q}" for i, q in enumerate(state.queued, 1))
            tooltip += f"\n\n{len(state.queued)} waiting:\n{waiting}"

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
        normalize,
        player,
        base_url,
        api_key_env,
        timeout,
        duck,
        duck_factor,
        pause_players,
        copy,
        enrich,
        style,
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
        # The level filter is an ffmpeg filter; the raw sinks have no filter
        # chain to put it in, so asking for both is a contradiction rather than
        # a knob that quietly does nothing.
        if normalize and player_mode is not PlayerMode.FFPLAY:
            raise click.UsageError(f"{player_mode.value} cannot normalize")

        match player_mode:
            case PlayerMode.FFPLAY:
                player_adapter: PlayerAdapter = PlayerAdapterFfplay(fmt, normalize)
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

        tts_style = TtsStyle(style)
        if tts_style is not TtsStyle.READ and not enrich:
            raise click.UsageError(f"--style {tts_style.value} needs --enrich")

        enricher: EnrichAdapter | None = None
        if enrich:
            enricher = build_enricher(
                spec_from_options(enrich_opts, "speech/1.0", "enrich"),
                Tts.system_prompt(tts_style),
                Tts.USER_PROMPT,
            )

        Tts(
            spec,
            input_adapter,
            player_adapter,
            copy,
            enricher,
            duck,
            duck_factor,
            pause_players,
        ).speak()

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
    create_logger(verbose, log_file="speech.log", quiet={"status", "is-recording", "is-speaking"})
    set_headless(headless)

cli.add_command(Stt.cli, "stt")
cli.add_command(Tts.cli, "tts")

if __name__ == "__main__":
    cli()
