"""Speech-to-text backends.

Three behind one `capture() -> str | None` call, all against an
OpenAI-compatible endpoint: a batch one transcribing whatever payload an
input adapter hands over, a microphone one that captures first and posts
what it recorded, and a realtime one streaming over a websocket. All
three are configured through an `SttSpec`.

The endpoint decodes whatever container the recorder produced, so
nothing is transcoded on the way out."""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
import queue
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


class RealtimeUnavailable(RuntimeError):
    """The realtime socket could not do the job it was asked for.

    Raised rather than quietly recording for batch. The two produce the same
    transcript, so a fallback is invisible from the outside, and a run that
    was never realtime looks exactly like realtime that found nothing to
    say."""


class SttProvider(StrEnum):
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
# The server defaults to 0.9 and 550ms, which splits a sentence at any pause
# for thought. Lower threshold, longer silence: fewer, whole turns.
DEFAULT_VAD_THRESHOLD = 0.5
DEFAULT_VAD_SILENCE_MS = 1200
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
    # Conditions the recogniser on style rather than vocabulary. Not an
    # instruction — the model treats it as preceding text, so it is a short
    # sample of well-punctuated technical prose: sentence case, real
    # punctuation, acronyms in capitals, digits for numbers. Deliberately
    # names no products; a list of them biases toward those and helps nothing
    # else, and the ones tried still came back wrong.
    prompt: str = ""
    timeout: float = DEFAULT_STT_TIMEOUT
    user_agent: str = "stt/1.0"
    # Server-side turn detection, for the realtime backend. The threshold is
    # a speech probability rather than a level, so a quiet but clear voice
    # scores lower than a loud one and needs a lower bar, not more gain.
    vad_threshold: float = DEFAULT_VAD_THRESHOLD
    vad_silence_ms: int = DEFAULT_VAD_SILENCE_MS

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
        if spec.prompt:
            fields.append(("prompt", spec.prompt))
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

    Every take is also kept whole in memory, which is what serves a take too
    short for the server to close a turn on at all. Anything else the socket
    cannot do is a failure and raises: a transcript that arrived by another
    route is indistinguishable from a realtime one, and hides the fault."""

    provider = SttProvider.REALTIME

    # 100ms frames: small enough that the server's VAD sees speech begin
    # promptly, large enough not to spend the take in syscalls.
    CHUNK_MS = 100
    # A turn closes on a pause of well under a second, but not in the first
    # three seconds of a take: speaches only ends one when the last speech in
    # its rolling 3s window has stopped AND the buffer already exceeds 3s
    # (`input_audio_buffer_event_router.vad_detection_flow`, its own `FIX:
    # magic number`). So the opening sentence is always one turn however it is
    # spoken, and every pause after that splits. Neither `silence_duration_ms`
    # nor `prefix_padding_ms` moves that floor; both were measured against it.
    # Silence pushed after the microphone closes so the server's own detector
    # ends the last turn. Added to the configured silence rather than fixed —
    # a shorter tail than the detector waits for would never close it.
    TAIL_MARGIN_MS = 400
    # How long to wait for the last turn to arrive and be handed over.
    STOP_DEADLINE = 8.0
    # Quiet spell after the last segment that counts as the take being over.
    SETTLE_SECONDS = 1.5
    # Below this the server cannot close a turn at all, whatever is said into
    # it, so a take this short yielding nothing is expected rather than broken.
    MIN_TURN_SECONDS = 3.0

    def __init__(self, spec: SttSpec, mic: MicCapture | None = None):
        self.spec = spec
        self.mic = mic or MicCapture()
        self.model = spec.model or DEFAULT_STT_MODEL
        self._stopped = threading.Event()
        # Why the socket stopped being usable, if it did. A drop mid-take is
        # as much a failure as one at the handshake, and it used to end the
        # recording while the take still looked like a success.
        self.socket_error: str | None = None
        self._closing = False
        self._segments: list[str] = []
        # Turns reach the subscriber from here rather than from `_receive`.
        # A subscriber may type its turn, which for ydotool is seconds, and
        # a receive thread inside that is a receive thread not reading the
        # socket: the turns behind it are lost when the socket closes.
        self._delivery: queue.Queue[str] = queue.Queue()
        self._delivered = 0
        self._on_segment: Callable[[str], None] | None = None
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
                if not self._closing:
                    log.error("realtime socket dropped: %s", e)
                    self.socket_error = str(e) or e.__class__.__name__
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
                    # The detector re-emits a turn it has already closed if
                    # the tail arrives faster than its own window, so an
                    # immediate repeat is the socket stuttering rather than
                    # the speaker saying it twice.
                    if self._segments and self._segments[-1] == text:
                        continue
                    self._segments.append(text)
                log.info("segment: %s", text)
                self._delivery.put(text)
            elif kind == "error":
                log.warning("realtime error: %s", (event.get("error") or {}))

    def _deliver(self) -> None:
        """Hand turns to the subscriber, one at a time, off the socket thread."""
        while True:
            text = self._delivery.get()
            if not text:
                return
            if self._on_segment:
                try:
                    self._on_segment(text)
                except OSError as e:
                    log.warning("subscriber refused a turn: %s", e)
            self._delivered += 1

    def _pump(self, ws) -> None:
        """Send the take as it is recorded, then silence to close the turn."""
        sent = 0
        chunk = int(self.mic.rate * self.mic.SAMPLE_BYTES * self.CHUNK_MS / 1000)
        while not self._stopped.is_set():
            pcm = self.mic.pcm()
            while len(pcm) - sent >= chunk:
                if not self._send_audio(ws, pcm[sent : sent + chunk]):
                    return
                sent += chunk
            time.sleep(self.CHUNK_MS / 1000)

        pcm = self.mic.pcm()
        while sent < len(pcm):
            if not self._send_audio(ws, pcm[sent : sent + chunk]):
                return
            sent += chunk
        # The detector only runs on arriving audio, so silence has to be sent
        # for it to notice the talking stopped. No manual commit: committing
        # mid-speech trips a server assertion and drops the socket.
        # Paced like the capture it follows. Sent as fast as the socket takes
        # it, the detector reads the whole tail inside one of its own windows
        # and re-emits the turn it already closed, once per frame.
        tail_ms = self.spec.vad_silence_ms + self.TAIL_MARGIN_MS
        for _ in range(int(tail_ms / self.CHUNK_MS)):
            if not self._send_audio(ws, b"\0" * chunk):
                return
            time.sleep(self.CHUNK_MS / 1000)

    def _send_audio(self, ws, pcm: bytes) -> bool:
        """Push one frame; False once the socket has stopped taking them."""
        try:
            ws.send(
                json.dumps(
                    {
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(pcm).decode(),
                    }
                )
            )
        except (OSError, websocket.WebSocketException) as e:
            if not self._closing:
                log.error("realtime send failed: %s", e)
                self.socket_error = str(e) or e.__class__.__name__
            return False

        return True

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
                                "threshold": self.spec.vad_threshold,
                                "silence_duration_ms": self.spec.vad_silence_ms,
                            }
                        },
                    }
                )
            )
        except (OSError, websocket.WebSocketException) as e:
            self.socket_error = str(e) or e.__class__.__name__
            self.mic.cancel()
            raise RealtimeUnavailable(f"socket refused: {self.socket_error}") from e

        threading.Thread(target=self._receive, args=(ws,), daemon=True).start()
        deliver = threading.Thread(target=self._deliver, daemon=True)
        deliver.start()
        pump = threading.Thread(target=self._pump, args=(ws,), daemon=True)
        pump.start()
        self._stopped.wait()
        pump.join(timeout=self.STOP_DEADLINE)

        # Waits for a quiet spell after the last turn reached the subscriber,
        # not after it arrived: a subscriber that types its turn is still
        # working long after the socket handed it over, and closing under it
        # loses whatever the socket had not been read for yet.
        deadline = time.monotonic() + self.STOP_DEADLINE
        counted, since = -1, time.monotonic()
        while time.monotonic() < deadline:
            done = self._delivered
            if done != counted:
                counted, since = done, time.monotonic()
            elif (
                done
                and self._delivery.empty()
                and time.monotonic() - since > self.SETTLE_SECONDS
            ):
                break
            time.sleep(0.2)

        self._closing = True
        try:
            ws.close()
        except OSError as e:
            log.debug("closing the realtime socket failed: %s", e)

        # Everything handed over before the caller is told the take is done,
        # so a dismiss or a chime cannot land ahead of the last keystroke.
        self._delivery.put("")
        deliver.join(timeout=self.STOP_DEADLINE)

        if self.socket_error:
            raise RealtimeUnavailable(f"socket dropped: {self.socket_error}")

        with self._lock:
            segments = list(self._segments)

        if not segments:
            # A take shorter than the server's own floor cannot close a turn,
            # so batch is the only thing that could ever have worked and this
            # is not a fault. Longer than that, silence from the socket is.
            seconds = len(self.mic.pcm()) / self.mic.rate / self.mic.SAMPLE_BYTES
            if seconds > self.MIN_TURN_SECONDS:
                raise RealtimeUnavailable(
                    f"no turns in {seconds:.1f}s of audio"
                )
            log.info("%.1fs is under the turn floor; posting it whole", seconds)
            return SttAdapterHttp(self.spec, self.mic).capture()

        return " ".join(segments).strip()

