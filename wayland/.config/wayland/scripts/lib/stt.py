"""Speech-to-text backends.

Two backends behind one `capture() -> str | None` call: hyprwhspr
driving the microphone locally, and an OpenAI-compatible HTTP endpoint
transcribing a payload an input adapter hands over. Callers configure
the HTTP one through an `SttSpec`.

The endpoint decodes whatever container the recorder produced, so
nothing is transcoded on the way out."""

from __future__ import annotations

import logging
import mimetypes
import os
import subprocess
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from .enrich import DEFAULT_API_KEY_ENV, DEFAULT_BASE_URL
from .input import InputAdapter


class SttProvider(StrEnum):
    HYPRWHSPR = "hyprwhspr"
    HTTP = "http"


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
        capture = subprocess.Popen(
            ["hyprwhspr", "record", "capture"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        stdout, _ = capture.communicate()

        return stdout.decode("utf-8", errors="replace").strip() if stdout else None


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
