"""Text-to-speech synthesis and playback.

One `synth(text)` context manager opening an audio stream, and one
`play(stream, sample_rate)` draining it into a local sink. The two stay
separate so the HTTP body is never buffered whole: the player's read
rate drives the socket, so the first samples reach the speakers while
the backend is still generating the tail.

Defaults are raw s16le PCM through ffplay — no container, no
server-side transcode, nothing to demux before the first sample."""

from __future__ import annotations

import io
import json
import logging
import os
import subprocess
import sys
import urllib.error
import urllib.request
import wave
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from .enrich import DEFAULT_API_KEY_ENV, DEFAULT_BASE_URL


class AudioFormat(StrEnum):
    PCM = "pcm"
    MP3 = "mp3"
    WAV = "wav"
    FLAC = "flac"


class PlayerMode(StrEnum):
    FFPLAY = "ffplay"
    PW_CAT = "pw-cat"
    PAPLAY = "paplay"


DEFAULT_TTS_MODEL = "kilic.dev/tts"
# American male — the least robotic of the Kokoro voices.
DEFAULT_TTS_VOICE = "am_michael"
DEFAULT_TTS_SAMPLE_RATE = 24000
DEFAULT_TTS_MAX_CHARS = 5000
DEFAULT_TTS_TIMEOUT = 120.0
DEFAULT_TTS_PLAYER = PlayerMode.FFPLAY

# Big enough that the pump is not syscall-bound, small enough that the
# first chunk lands at the sink well inside a human's patience.
CHUNK_BYTES = 1 << 15

log = logging.getLogger(__name__)


@dataclass
class TtsSpec:
    """Every knob the synthesis backend accepts, in one shape.

    The API key travels as the *name* of an env var, never the secret —
    the adapter resolves it at call time."""

    model: str | None = None
    voice: str = DEFAULT_TTS_VOICE
    speed: float = 1.0
    response_format: AudioFormat = AudioFormat.PCM
    sample_rate: int = DEFAULT_TTS_SAMPLE_RATE
    base_url: str = DEFAULT_BASE_URL
    api_key_env: str = DEFAULT_API_KEY_ENV
    timeout: float = DEFAULT_TTS_TIMEOUT
    max_chars: int = DEFAULT_TTS_MAX_CHARS
    user_agent: str = "tts/1.0"


class ByteStream(Protocol):
    """Readable byte source — the slice of a file object the pump uses."""

    def read(self, size: int = -1) -> bytes: ...


class TtsAdapterHttp:
    """OpenAI-compatible `/audio/speech` endpoint (speaches, Kokoro)."""

    DEFAULT_MODEL = DEFAULT_TTS_MODEL

    def __init__(self, spec: TtsSpec):
        self.spec = spec
        self.model = spec.model or self.DEFAULT_MODEL

    @contextmanager
    def synth(self, text: str) -> Iterator[ByteStream]:
        spec = self.spec
        body: dict[str, Any] = {
            "model": self.model,
            "input": text,
            "voice": spec.voice,
            "response_format": spec.response_format.value,
            "sample_rate": spec.sample_rate,
            "speed": spec.speed,
        }
        payload = json.dumps(body)
        log.debug("request: %s", payload)
        req = urllib.request.Request(
            f"{spec.base_url}/audio/speech",
            data=payload.encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {os.environ.get(spec.api_key_env, '')}",
                "User-Agent": spec.user_agent,
            },
        )
        try:
            resp = urllib.request.urlopen(req, timeout=spec.timeout)
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

        log.info("synthesis stream open (model=%s voice=%s)", self.model, spec.voice)
        # Yielded rather than read(): the player drains it chunk by chunk, so
        # a read() here would buffer the whole utterance before a single
        # sample reaches the sink.
        with resp:
            yield resp


class TeeReader:
    """Readable wrapper mirroring everything read into `sink`.

    Lets `--copy` keep the whole utterance without buffering it ahead of
    playback — the player still drives the read rate."""

    def __init__(self, source: ByteStream, sink: io.BytesIO):
        self._source = source
        self._sink = sink

    def read(self, size: int = -1) -> bytes:
        chunk = self._source.read(size)
        if chunk:
            self._sink.write(chunk)
        return chunk


class PlayerAdapter(Protocol):
    """Local audio sink fed from a byte stream."""

    mode: PlayerMode

    def play(self, stream: ByteStream, sample_rate: int) -> tuple[int, int]:
        """Drain `stream` into the sink; returns (bytes played, exit code).

        The exit code comes back rather than being logged and dropped so
        the caller can tell the user that playback failed."""
        ...


def _stream_to_player(cmd: list[str], stream: ByteStream) -> tuple[int, int]:
    """Pump `stream` into `cmd`'s stdin, returning (bytes written, exit code).

    The player deliberately stays in our process group so the session's
    `killpg` reaches it. A BrokenPipeError only means it exited first —
    killed mid-utterance, or `-autoexit` beating the tail of the body.

    The `wait()` is deliberately untimed: it blocks for exactly as long as
    the audio lasts, which `--max-chars` already bounds. The session's
    socket thread keeps answering while it runs, so `tts kill` stays the
    escape hatch for a player that never returns."""
    log.debug("spawn: %s", " ".join(cmd))
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=sys.stderr,
        stderr=sys.stderr,
    )
    assert proc.stdin is not None

    written = 0
    try:
        while chunk := stream.read(CHUNK_BYTES):
            proc.stdin.write(chunk)
            written += len(chunk)
    except BrokenPipeError:
        log.debug("player closed the pipe after %d bytes", written)
    finally:
        try:
            proc.stdin.close()
        except BrokenPipeError:
            pass
        proc.wait()

    return written, proc.returncode


class PlayerAdapterFfplay:
    """ffmpeg's player — the only sink that can demux a container."""

    mode = PlayerMode.FFPLAY

    def __init__(self, response_format: AudioFormat = AudioFormat.PCM):
        self.response_format = response_format

    def play(self, stream: ByteStream, sample_rate: int) -> tuple[int, int]:
        cmd = [
            "ffplay",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nodisp",
            "-autoexit",
        ]
        # Raw PCM carries no rate or channel count, so it has to be declared;
        # every other format is self-describing and probing it is enough.
        # `-ac` is gone as of ffmpeg 9 — the pcm demuxer only knows
        # `ch_layout` since the AVChannelLayout migration.
        if self.response_format is AudioFormat.PCM:
            cmd += ["-f", "s16le", "-ar", str(sample_rate), "-ch_layout", "mono"]
        cmd += ["-i", "pipe:0"]
        return _stream_to_player(cmd, stream)


class PlayerAdapterPwCat:
    """PipeWire's own sink. Raw s16le only."""

    mode = PlayerMode.PW_CAT

    def play(self, stream: ByteStream, sample_rate: int) -> tuple[int, int]:
        cmd = [
            "pw-cat",
            "-p",
            "--raw",
            "--format",
            "s16",
            "--rate",
            str(sample_rate),
            "--channels",
            "1",
            "-",
        ]
        return _stream_to_player(cmd, stream)


class PlayerAdapterPaplay:
    """PulseAudio compatibility sink. Raw s16le only.

    No file argument: `paplay` is libpulse's `pacat`, which reads stdin
    only when none is given and opens a literal `-` as a filename."""

    mode = PlayerMode.PAPLAY

    def play(self, stream: ByteStream, sample_rate: int) -> tuple[int, int]:
        cmd = [
            "paplay",
            "--raw",
            "--format=s16le",
            f"--rate={sample_rate}",
            "--channels=1",
        ]
        return _stream_to_player(cmd, stream)


_CLIPBOARD_MIMES = {
    AudioFormat.PCM: "audio/wav",
    AudioFormat.WAV: "audio/wav",
    AudioFormat.MP3: "audio/mpeg",
    AudioFormat.FLAC: "audio/flac",
}


def copy_audio(data: bytes, spec: TtsSpec) -> None:
    """Put the synthesized audio on the clipboard.

    Raw PCM gets a WAV header first — a bare s16le blob names neither
    its rate nor its channel count, so whatever pastes it has nothing to
    play back."""
    mime = _CLIPBOARD_MIMES[spec.response_format]
    if spec.response_format is AudioFormat.PCM:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(spec.sample_rate)
            wav.writeframes(data)
        data = buf.getvalue()

    cmd = ["wl-copy", "--type", mime]
    log.debug("spawn: %s (%d bytes)", " ".join(cmd), len(data))
    subprocess.run(
        cmd,
        input=data,
        check=False,
        stdout=sys.stderr,
        stderr=sys.stderr,
    )
