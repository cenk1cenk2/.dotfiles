"""Speech-to-text backends.

Three behind one `capture() -> str | None` call, all against an
OpenAI-compatible endpoint: a batch one transcribing whatever payload an
input adapter hands over, a microphone one that captures first and posts
what it recorded, and a realtime one that captions over a websocket while
recording and then posts the take like the microphone one. All three are
configured through an `SttSpec`.

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
from dotlib.enrich import DEFAULT_API_KEY_ENV, DEFAULT_BASE_URL

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
# The live captions come from a pipeline model of their own, apart from the
# batch one that produces the text.
REALTIME_MODEL = "kilic.dev/stt/realtime"
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
    # Conditions the recogniser on style rather than vocabulary. Not an
    # instruction — the model treats it as preceding text, so it is a short
    # sample of well-punctuated technical prose: sentence case, real
    # punctuation, acronyms in capitals, digits for numbers. Deliberately
    # names no products; a list of them biases toward those and helps nothing
    # else, and the ones tried still came back wrong.
    prompt: str = ""
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

    def pause(self) -> bool: ...

    def muted(self) -> bool: ...


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
class SttPartials(SttRecorder, Protocol):
    """Recorder that reports the live, revisable transcript during a take.

    The text handed to `on_partial` is the whole line so far and is replaced
    by the next call, so it is for display only: whatever it said can be
    taken back, and the take's real text is what `capture` returns."""

    def subscribe_partials(self, on_partial: Callable[[str], None]) -> None: ...


@runtime_checkable
class SttTurns(SttRecorder, Protocol):
    """Recorder whose closed turns are the take's text, as they close.

    Nothing handed to `on_turn` is revised afterwards, so it can go straight
    to a sink; `capture` returns the same turns joined."""

    def subscribe_turns(self, on_turn: Callable[[str], None]) -> None: ...


class SttAdapterHttp:
    """OpenAI-compatible `/audio/transcriptions` endpoint."""

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
        # The server names its plain-text format `txt` and answers 400 to `text`.
        response_format = (
            "txt"
            if spec.response_format is ResponseFormat.TEXT
            else spec.response_format.value
        )
        fields = [
            ("model", self.model),
            ("response_format", response_format),
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

    def pause(self) -> bool:
        return self.mic.pause()

    def muted(self) -> bool:
        return self.mic.muted()

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
    """Live captions over the realtime socket, with the batch path for the text.

    The same capture as `SttAdapterMic` — 24 kHz mono s16 is the wire format,
    which is why the recorder runs at that rate — pushed at the socket in
    100ms frames while it is being recorded. Its deltas and turns go to the
    `subscribe_partials` callback. With `batch` the text of the take is the
    whole recording posted to the batch endpoint when it ends: the streaming
    model emits lowercase, unpunctuated turns, which the batch one does not.
    Without it the text is the socket's own turns, each handed to
    `subscribe_turns` as it closes.

    A socket that cannot do its job is a failure and raises: a transcript
    that arrived by another route is indistinguishable from a realtime one,
    and hides the fault."""

    provider = SttProvider.REALTIME

    # 100ms frames: small enough that the server's VAD sees speech begin
    # promptly, large enough not to spend the take in syscalls.
    CHUNK_MS = 100
    # How long to wait for the sender to drain once the microphone closes, and
    # again for the last turn to close.
    STOP_DEADLINE = 8.0
    # The detector only runs on arriving audio, so silence is sent after the
    # microphone closes for it to notice the talking stopped.
    TAIL_MS = 1500
    # Quiet spell after the last turn event that counts as the take being over.
    SETTLE_SECONDS = 1.0

    def __init__(
        self,
        spec: SttSpec,
        mic: MicCapture | None = None,
        batch: bool = True,
    ):
        self.spec = spec
        self.mic = mic or MicCapture()
        self.batch = batch
        self._stopped = threading.Event()
        # Why the socket stopped being usable, if it did. A drop mid-take is
        # as much a failure as one at the handshake, and it used to end the
        # recording while the take still looked like a success.
        self.socket_error: str | None = None
        self._closing = False
        self._on_partial: Callable[[str], None] | None = None
        # Turns by item id, in arrival order: a delta extends its turn,
        # `completed` replaces it and `failed` retracts it.
        self._items: dict[str, str] = {}
        # Turns that have deltas but no `completed` or `failed` yet.
        self._open: set[str] = set()
        self._last_event = time.monotonic()
        # Closed turns reach the subscriber from here rather than from
        # `_receive`: a subscriber may take seconds over a turn, and a receive
        # thread inside it is not reading the socket.
        self._delivery: queue.Queue[str | None] = queue.Queue()
        self._on_turn: Callable[[str], None] | None = None
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

    def pause(self) -> bool:
        return self.mic.pause()

    def muted(self) -> bool:
        return self.mic.muted()

    def frame(self) -> tuple[float, list[float]] | None:
        return self.mic.frame()

    def subscribe_partials(self, on_partial: Callable[[str], None]) -> None:
        self._on_partial = on_partial

    def subscribe_turns(self, on_turn: Callable[[str], None]) -> None:
        self._on_turn = on_turn

    # ── the socket ────────────────────────────────────────────────

    def _url(self) -> str:
        base = self.spec.base_url.replace("https://", "wss://", 1).replace(
            "http://", "ws://", 1
        )
        query = urllib.parse.urlencode({"model": REALTIME_MODEL})

        return f"{base}/realtime?{query}"

    def _session(self) -> dict:
        transcription = {"model": REALTIME_MODEL}
        if self.spec.language:
            transcription["language"] = self.spec.language

        # `turn_detection` has to be sent, and `session.updated` never echoes it.
        return {
            "type": "session.update",
            "session": {
                "type": "transcription",
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": self.mic.rate},
                        "transcription": transcription,
                        "turn_detection": {
                            "type": "semantic_vad",
                            "eagerness": "high",
                        },
                    }
                },
            },
        }

    def _receive(self, ws) -> None:
        while True:
            try:
                raw = ws.recv()
            except (OSError, websocket.WebSocketException) as e:
                if not self._closing:
                    self._drop("realtime socket dropped", e)
                return
            if not raw:
                return
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue

            self._caption(event.get("type", ""), event)

    def _caption(self, kind: str, event: dict) -> None:
        """Fold a transcription event into the live line."""
        if kind == "error":
            log.warning("realtime error: %s", (event.get("error") or {}))
            return
        if not (item := event.get("item_id")):
            return

        with self._lock:
            if kind.endswith("input_audio_transcription.delta"):
                self._items[item] = self._items.get(item, "") + (
                    event.get("delta") or ""
                )
                self._open.add(item)
            elif kind.endswith("input_audio_transcription.completed"):
                self._items[item] = event.get("transcript") or ""
                self._open.discard(item)
                if not self.batch and (turn := self._items[item].strip()):
                    self._delivery.put(turn)
            elif kind.endswith("input_audio_transcription.failed"):
                self._items.pop(item, None)
                self._open.discard(item)
            else:
                return
            self._last_event = time.monotonic()
            line = self._line()
        if self._on_partial:
            self._on_partial(line)

    def _line(self) -> str:
        return " ".join(text for turn in self._items.values() if (text := turn.strip()))

    def _deliver(self) -> None:
        """Hand closed turns to the subscriber, one at a time, off the socket."""
        while (turn := self._delivery.get()) is not None:
            if self._on_turn:
                try:
                    self._on_turn(turn)
                except OSError as e:
                    log.warning("subscriber refused a turn: %s", e)
            self._delivery.task_done()

    def _settle(self) -> None:
        """Wait for the last turn to close, reach the subscriber, and the
        socket to go quiet."""
        deadline = time.monotonic() + self.STOP_DEADLINE
        while not self.socket_error and time.monotonic() < deadline:
            with self._lock:
                if (
                    not self._open
                    and not self._delivery.unfinished_tasks
                    and time.monotonic() - self._last_event >= self.SETTLE_SECONDS
                ):
                    return
            time.sleep(self.CHUNK_MS / 1000)
        log.warning("realtime turns still open at the deadline")

    def _pump(self, ws) -> None:
        """Send the take as it is recorded."""
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
        if self.batch:
            return
        # Paced like the capture it follows, so the detector reads it as time
        # passing rather than one burst.
        for _ in range(self.TAIL_MS // self.CHUNK_MS):
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
                self._drop("realtime send failed", e)
            return False

        return True

    def _drop(self, what: str, error: Exception) -> None:
        """End the take the moment the socket stops being usable.

        Nothing reconnects, so every second recorded past a drop is speech
        that can never be transcribed. Stopping here wakes `capture`, which
        raises while the speaker is still mid-thought rather than at the
        next toggle."""
        with self._lock:
            if self.socket_error:
                return
            self.socket_error = str(error) or error.__class__.__name__
        log.error("%s: %s", what, error)
        self.stop()

    # ── capture ───────────────────────────────────────────────────

    def capture(self) -> str | None:
        self.mic.start()
        ws = None
        try:
            ws = websocket.create_connection(
                self._url(),
                header=[
                    f"Authorization: Bearer {os.environ.get(self.spec.api_key_env, '')}",
                    *(f"{name}: {value}" for name, value in self.spec.headers),
                ],
                timeout=30,
                suppress_origin=True,
            )
            ws.send(json.dumps(self._session()))
        except (OSError, websocket.WebSocketException) as e:
            self.socket_error = str(e) or e.__class__.__name__
            self.mic.cancel()
            raise RealtimeUnavailable(f"socket refused: {self.socket_error}") from e

        threading.Thread(target=self._receive, args=(ws,), daemon=True).start()
        threading.Thread(target=self._deliver, daemon=True).start()
        pump = threading.Thread(target=self._pump, args=(ws,), daemon=True)
        pump.start()
        self._stopped.wait()
        pump.join(timeout=self.STOP_DEADLINE)
        if not self.batch:
            self._settle()
        self._delivery.put(None)

        self._closing = True
        try:
            ws.close()
        except OSError as e:
            log.debug("closing the realtime socket failed: %s", e)

        if self.socket_error:
            raise RealtimeUnavailable(f"socket dropped: {self.socket_error}")
        if not self.mic.pcm():
            log.error("capture held no audio")
            return None
        if not self.batch:
            with self._lock:
                return self._line()

        return SttAdapterHttp(self.spec, self.mic).capture()
