"""Speech-to-text backends.

Two backends behind one `capture() -> str | None` call: hyprwhspr
driving the microphone locally, and an OpenAI-compatible HTTP endpoint
transcribing a payload an input adapter hands over. Callers configure
the HTTP one through an `SttSpec`.

The endpoint decodes whatever container the recorder produced, so
nothing is transcoded on the way out."""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

import websocket

from .enrich import DEFAULT_API_KEY_ENV, DEFAULT_BASE_URL
from .input import InputAdapter, MicCapture


class SttProvider(StrEnum):
    HYPRWHSPR = "hyprwhspr"
    HTTP = "http"
    MIC = "mic"
    REALTIME = "realtime"


class ResponseFormat(StrEnum):
    TEXT = "text"
    JSON = "json"
    VERBOSE_JSON = "verbose_json"
    SRT = "srt"
    VTT = "vtt"


DEFAULT_STT_MODEL = "kilic.dev/stt"
DEFAULT_STT_TIMEOUT = 300.0
# One ISO-639-1 code: the endpoint answers 500 to a list or an unknown
# code. Empty leaves the field off, which is the model's auto-detect.
DEFAULT_STT_LANGUAGE = "en"
# The one format that is prose, so the only one enrichment can rewrite:
# a cleanup pass over the others would eat the timings and the structure.
PLAIN_FORMATS = (ResponseFormat.TEXT,)

log = logging.getLogger(__name__)


@dataclass
class SttSpec:
    """Every knob the transcription backend accepts, in one shape.

    The API key travels as the *name* of an env var, never the secret —
    the adapter resolves it at call time."""

    model: str | None = None
    base_url: str = DEFAULT_BASE_URL
    api_key_env: str = DEFAULT_API_KEY_ENV
    response_format: ResponseFormat = ResponseFormat.TEXT
    language: str = DEFAULT_STT_LANGUAGE
    timeout: float = DEFAULT_STT_TIMEOUT
    user_agent: str = "stt/1.0"

    # Whatever else this backend takes, as ordered name/value pairs rather
    # than a mapping: a repeated name is how the OpenAI shape spells a list
    # (`timestamp_granularities[]` twice), so the duplicates are load-bearing.
    fields: tuple[tuple[str, str], ...] = ()
    headers: tuple[tuple[str, str], ...] = ()


@runtime_checkable
class SttAdapter(Protocol):
    """Speech-to-text backend contract."""

    provider: SttProvider

    def capture(self) -> str | None:
        """Return the transcription, or None on failure."""
        ...


@runtime_checkable
class SttRecorder(SttAdapter, Protocol):
    """Backend that drives a microphone, so a capture can be in flight."""

    def is_recording(self) -> bool: ...

    def stop(self) -> None: ...

    def cancel(self) -> None: ...


@runtime_checkable
class LevelSource(Protocol):
    """Recorder that owns its capture loop, so it can report signal levels.

    Narrow and separate from `SttRecorder` because a backend driving someone
    else's daemon has no access to the samples: the overlay asks with
    `isinstance` and draws a flat row when the answer is no."""

    def frame(self) -> tuple[float, list[float]] | None:
        """Current (peak, bars), or None before anything has been captured."""
        ...


@runtime_checkable
class SttStreaming(SttRecorder, Protocol):
    """Recorder that yields finalised segments while the capture runs.

    Segments only — the endpoint publishes no partial deltas, so nothing
    handed to `on_segment` is ever revised or retracted."""

    def subscribe(self, on_segment: Callable[[str], None]) -> None: ...


class SttAdapterHyprwhspr:
    provider = SttProvider.HYPRWHSPR

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
            ["hyprwhspr", "record", "stop"], capture_output=True, check=False
        )

    def cancel(self) -> None:
        subprocess.run(
            ["hyprwhspr", "record", "cancel"], capture_output=True, check=False
        )

    def capture(self) -> str | None:
        # Blocks until the recording stops, which is what makes `stop`
        # reachable only from the second press or the session socket.
        cmd = ["hyprwhspr", "record", "capture"]
        log.debug("spawn: %s", " ".join(cmd))
        capture = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, stderr = capture.communicate()
        message = stderr.decode("utf-8", errors="replace").strip()

        # A daemon that is down exits non-zero with nothing on stdout, which
        # is what silence looks like too: the returncode is the only thing
        # that tells the two apart.
        if capture.returncode != 0:
            raise subprocess.CalledProcessError(
                capture.returncode, cmd, output=stdout, stderr=message
            )

        log.debug("hyprwhspr stderr: %s", message)

        return stdout.decode("utf-8", errors="replace").strip()


class SttAdapterHttp:
    """OpenAI-compatible `/audio/transcriptions` endpoint (speaches, Whisper)."""

    provider = SttProvider.HTTP
    DEFAULT_MODEL = DEFAULT_STT_MODEL

    def __init__(self, spec: SttSpec, source: InputAdapter):
        self.spec = spec
        self.source = source
        self.model = spec.model or self.DEFAULT_MODEL

    def capture(self) -> str | None:
        spec = self.spec
        audio = self.source.read_bytes()
        if not audio:
            log.error("%s held no audio", self.source.mode.value)
            return None

        body, content_type = self._form(audio)
        req = urllib.request.Request(
            f"{spec.base_url}/audio/transcriptions",
            data=body,
            headers={
                "Content-Type": content_type,
                "Authorization": f"Bearer {os.environ.get(spec.api_key_env, '')}",
                "User-Agent": spec.user_agent,
                **dict(spec.headers),
            },
        )
        log.info(
            "transcribing %s (model=%s format=%s)",
            self.source.name,
            self.model,
            spec.response_format.value,
        )
        try:
            with urllib.request.urlopen(req, timeout=spec.timeout) as resp:
                raw = resp.read().decode(errors="replace")
        except urllib.error.HTTPError as e:
            # The error body is only readable here — the caller sees the
            # re-raised HTTPError with its stream already consumed.
            log.error(
                "HTTP %d (model=%s): %s",
                e.code,
                self.model,
                e.read().decode(errors="replace"),
            )
            raise

        log.debug("response: %s", raw)

        # Only `text` is the transcript itself. The other formats are for a
        # machine on the far end — the timings and per-segment fields are
        # the point — so they travel through exactly as the backend wrote
        # them rather than being reduced to their `text` key.
        return raw.strip()

    def _form(self, audio: bytes) -> tuple[bytes, str]:
        """Serialise the upload as multipart/form-data: body, content type.

        Written by hand rather than through `email.message.EmailMessage`,
        which is the obvious stdlib candidate and silently corrupts the
        upload: its generator rewrites every bare LF in a payload to CRLF,
        so a 50 KB Opus file arrives 400 bytes longer and the endpoint
        answers "Failed to decode audio". Nothing in that module can carry
        bytes through untouched, and no third-party HTTP client is a
        dependency here."""
        spec = self.spec
        name = self.source.name
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        boundary = f"----speech-stt-{uuid.uuid4().hex}"
        fields = [
            ("model", self.model),
            ("response_format", spec.response_format.value),
        ]
        if spec.language:
            fields.append(("language", spec.language))
        fields.extend(spec.fields)
        body = b"".join(
            [
                *(
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{field}"\r\n\r\n'
                    f"{value}\r\n".encode()
                    for field, value in fields
                ),
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n".encode(),
                audio,
                b"\r\n",
                f"--{boundary}--\r\n".encode(),
            ]
        )

        return body, f"multipart/form-data; boundary={boundary}"


class SttAdapterMic:
    """Our own microphone capture, posted to the same endpoint as `http`.

    Composition rather than a second HTTP client: `SttAdapterHttp` already
    takes any `InputAdapter` and `MicCapture` is one, so this adapter only
    owns the recording lifecycle and hands the bytes over."""

    provider = SttProvider.MIC

    def __init__(self, spec: SttSpec, mic: MicCapture | None = None):
        self.spec = spec
        self.mic = mic or MicCapture()
        # `capture` blocks on this the way the packaged recorder blocks on its
        # daemon, so the session's STOP handler ends a take by the same route
        # for either adapter.
        self._stopped = threading.Event()

    def is_recording(self) -> bool:
        return self.mic.is_recording()

    def stop(self) -> None:
        self.mic.stop()
        self._stopped.set()

    def cancel(self) -> None:
        self.mic.cancel()
        self._stopped.set()

    def frame(self) -> tuple[float, list[float]] | None:
        return self.mic.frame()

    def capture(self) -> str | None:
        self.mic.start()
        self._stopped.wait()
        if not self.mic.pcm():
            log.error("capture held no audio")
            return None

        return SttAdapterHttp(self.spec, self.mic).capture()


class SttAdapterRealtime:
    """Transcription over the realtime socket, with the batch path beneath it.

    The same capture as `SttAdapterMic` — 24 kHz mono s16 is the wire format,
    which is why the recorder runs at that rate — pushed at the socket in
    100ms frames while it is being recorded. The server closes a turn on its
    own silence detection and answers with one finalised segment per turn;
    there are no partial deltas, so nothing handed back is ever revised.

    Every take is also kept whole in memory. Anything the socket fails to
    account for is posted the ordinary way at the end, so a refused
    handshake, a mid-take drop or a turn that never closed costs a slower
    finish rather than the recording."""

    provider = SttProvider.REALTIME

    # 100ms frames: small enough that the server's VAD sees speech begin
    # promptly, large enough not to spend the take in syscalls.
    CHUNK_MS = 100
    # The server defaults to 0.9 and 550ms, which splits a sentence at any
    # pause for thought. Lower threshold, longer silence: fewer, whole turns.
    VAD_THRESHOLD = 0.5
    VAD_SILENCE_MS = 1200
    # Silence pushed after the microphone closes so the server's own detector
    # ends the last turn. Derived from the silence above rather than fixed —
    # a shorter tail than the detector waits for would never close it.
    TAIL_MARGIN_MS = 400
    # How long to wait for the final segment before giving up and posting.
    STOP_DEADLINE = 8.0
    # Below this, an unaccounted tail is not worth a second request.
    MIN_TAIL_SECONDS = 1.0

    def __init__(self, spec: SttSpec, mic: MicCapture | None = None):
        self.spec = spec
        self.mic = mic or MicCapture()
        self.model = spec.model or DEFAULT_STT_MODEL
        self._stopped = threading.Event()
        self._segments: list[tuple[int, str]] = []
        self._on_segment: Callable[[str], None] | None = None
        # Bytes of the take the server has accounted for, from its own
        # `audio_end_ms` rather than from how much we had sent when the event
        # arrived — that would over-run by the network and model latency and
        # the fallback would then skip the start of the next utterance.
        self._committed = 0
        self._lock = threading.Lock()

    # ── recorder contract ─────────────────────────────────────────

    def is_recording(self) -> bool:
        return self.mic.is_recording()

    def stop(self) -> None:
        self.mic.stop()
        self._stopped.set()

    def cancel(self) -> None:
        self.mic.cancel()
        self._stopped.set()

    def frame(self) -> tuple[float, list[float]] | None:
        return self.mic.frame()

    def subscribe(self, on_segment: Callable[[str], None]) -> None:
        self._on_segment = on_segment

    # ── the socket ────────────────────────────────────────────────

    def _url(self) -> str:
        base = self.spec.base_url.replace("https://", "wss://", 1).replace(
            "http://", "ws://", 1
        )
        query = urllib.parse.urlencode(
            {
                # `model` is required — without it the handshake is refused.
                "model": self.model,
                "intent": "transcription",
                "language": self.spec.language,
            }
        )

        return f"{base}/realtime?{query}"

    def _receive(self, ws) -> None:
        while True:
            try:
                raw = ws.recv()
            except (OSError, websocket.WebSocketException) as e:
                log.debug("realtime socket closed: %s", e)
                return
            if not raw:
                return
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue

            kind = event.get("type", "")
            if kind.endswith("input_audio_transcription.completed"):
                text = (event.get("transcript") or "").strip()
                if not text:
                    continue
                with self._lock:
                    self._segments.append((len(self._segments), text))
                log.info("segment: %s", text)
                if self._on_segment:
                    self._on_segment(text)
            elif kind == "input_audio_buffer.speech_stopped":
                end_ms = event.get("audio_end_ms")
                if isinstance(end_ms, int):
                    with self._lock:
                        self._committed = int(
                            end_ms * self.mic.rate * self.mic.SAMPLE_BYTES / 1000
                        )
            elif kind == "error":
                log.warning("realtime error: %s", (event.get("error") or {}))

    def _pump(self, ws) -> None:
        """Send the take as it is recorded, then silence to close the turn."""
        sent = 0
        chunk = int(self.mic.rate * self.mic.SAMPLE_BYTES * self.CHUNK_MS / 1000)
        while not self._stopped.is_set():
            pcm = self.mic.pcm()
            while len(pcm) - sent >= chunk:
                self._send_audio(ws, pcm[sent : sent + chunk])
                sent += chunk
            time.sleep(self.CHUNK_MS / 1000)

        pcm = self.mic.pcm()
        while sent < len(pcm):
            self._send_audio(ws, pcm[sent : sent + chunk])
            sent += chunk
        # The detector only runs on arriving audio, so silence has to be sent
        # for it to notice the talking stopped. No manual commit: committing
        # mid-speech trips a server assertion and drops the socket.
        for _ in range(int((self.VAD_SILENCE_MS + self.TAIL_MARGIN_MS) / self.CHUNK_MS)):
            self._send_audio(ws, b"\0" * chunk)

    def _send_audio(self, ws, pcm: bytes) -> None:
        ws.send(
            json.dumps(
                {
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(pcm).decode(),
                }
            )
        )

    # ── capture ───────────────────────────────────────────────────

    def capture(self) -> str | None:
        self.mic.start()
        ws = None
        try:
            ws = websocket.create_connection(
                self._url(),
                header=[
                    f"Authorization: Bearer {os.environ.get(self.spec.api_key_env, '')}"
                ],
                timeout=30,
                suppress_origin=True,
            )
            ws.send(
                json.dumps(
                    {
                        "type": "session.update",
                        "session": {
                            "turn_detection": {
                                "type": "server_vad",
                                "threshold": self.VAD_THRESHOLD,
                                "silence_duration_ms": self.VAD_SILENCE_MS,
                            }
                        },
                    }
                )
            )
        except (OSError, websocket.WebSocketException) as e:
            log.warning("realtime unavailable (%s); recording for batch", e)
            ws = None

        if ws is None:
            self._stopped.wait()
            return self._batch(0)

        threading.Thread(target=self._receive, args=(ws,), daemon=True).start()
        pump = threading.Thread(target=self._pump, args=(ws,), daemon=True)
        pump.start()
        self._stopped.wait()
        pump.join(timeout=self.STOP_DEADLINE)

        deadline = time.monotonic() + self.STOP_DEADLINE
        while time.monotonic() < deadline:
            with self._lock:
                covered = self._committed
            if covered and covered >= len(self.mic.pcm()) - self.mic.rate:
                break
            time.sleep(0.2)

        try:
            ws.close()
        except OSError as e:
            log.debug("closing the realtime socket failed: %s", e)

        with self._lock:
            segments = [text for _, text in self._segments]
            covered = self._committed

        tail = len(self.mic.pcm()) - covered
        if tail > self.MIN_TAIL_SECONDS * self.mic.rate * self.mic.SAMPLE_BYTES:
            log.info("%.1fs unaccounted for; posting the tail", tail / self.mic.rate / self.mic.SAMPLE_BYTES)
            rest = self._batch(covered)
            if rest:
                segments.append(rest)

        if not segments:
            log.warning("realtime returned nothing; posting the whole take")
            return self._batch(0)

        return " ".join(segments).strip()

    def _batch(self, offset: int) -> str | None:
        """Post the take from `offset` the ordinary way."""
        pcm = self.mic.pcm()[offset:]
        if not pcm:
            return None
        source = MicCapture(rate=self.mic.rate)
        source._pcm = bytearray(pcm)

        return SttAdapterHttp(self.spec, source).capture()
