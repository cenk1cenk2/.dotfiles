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
from collections.abc import Callable, Iterator
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
DEFAULT_TTS_TIMEOUT = 120.0
DEFAULT_TTS_PLAYER = PlayerMode.FFPLAY
# Kokoro hands back about -23 LUFS, some 7 LU under a normal speech target and
# audibly thin against anything else on the desktop. This lifts it to about
# -17.8 LUFS at -0.3 dBTP.
#
# `speechnorm` rather than the obvious alternatives, each of which fails on
# material that peaks at -3.3 dBTP with almost no dynamic range:
#   - plain `volume` reaches the loudness but clips, and `alimiter` does not
#     save it because it bounds sample peak rather than true peak
#   - `loudnorm` is louder still and peak-clean, but its dynamic mode carries
#     a multi-second lookahead, and `ffplay -autoexit` quits at input EOF
#     without draining it. That silently cut every utterance short - a 5.7s
#     sample played for 2.9s.
# `speechnorm` has no lookahead, so playback keeps its full length.
SPEECHNORM_FILTER = "speechnorm=e=6.25:r=0.00001:l=1"

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


class PrefixReader:
    """Serves `prefix` bytes, then everything from `source`.

    The prefix sits outside any tee the caller wrapped around `source`, so a
    chime is heard but never lands in the copied audio."""

    def __init__(self, prefix: bytes, source: ByteStream):
        self._prefix = memoryview(prefix)
        self._source = source

    def read(self, size: int = -1) -> bytes:
        if self._prefix:
            if size < 0:
                chunk, self._prefix = bytes(self._prefix), memoryview(b"")
                return chunk + self._source.read(size)
            chunk = bytes(self._prefix[:size])
            self._prefix = self._prefix[size:]
            return chunk

        return self._source.read(size)


class LevelReader:
    """Readable wrapper holding the peak level of the samples passing through.

    Per chunk, so the reading is what is audible now rather than for the whole
    utterance. Raw s16le only: a container's bytes are not samples, so a format
    that carries one leaves the level at zero."""

    def __init__(self, source: ByteStream):
        self._source = source
        self.peak = 0.0

    def read(self, size: int = -1) -> bytes:
        chunk = self._source.read(size)
        if chunk and len(chunk) % 2 == 0:
            # Some sixteen thousand samples a chunk, on the path feeding the
            # player, so the scan stays inside the memoryview.
            samples = memoryview(chunk).cast("h")
            self.peak = max(max(samples), -min(samples)) / 32768

        return chunk


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


class OnsetReader:
    """Readable wrapper firing `on_onset` once, as the first bytes pass.

    Where "the audio started" actually is: the pump only pulls once the
    player is up and taking samples, so the first chunk through here is the
    first that can be heard. The response opening is no answer - the headers
    land while the backend is still generating the first word."""

    def __init__(self, source: ByteStream, on_onset: Callable[[], None]):
        self._source = source
        self._on_onset = on_onset
        self._fired = False

    def read(self, size: int = -1) -> bytes:
        chunk = self._source.read(size)
        if chunk and not self._fired:
            self._fired = True
            self._on_onset()

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
    the audio lasts. The session's socket thread keeps answering while it
    runs, so `tts kill` stays the escape hatch for a player that never
    returns."""
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
    """ffmpeg's player — the only sink that can demux a container.

    Also the only one that can normalise: the level filter is an ffmpeg
    filter, and the raw sinks below take a linear volume at best."""

    mode = PlayerMode.FFPLAY

    def __init__(
        self,
        response_format: AudioFormat = AudioFormat.PCM,
        normalize: bool = True,
    ):
        self.response_format = response_format
        self.normalize = normalize

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
        if self.normalize:
            cmd += ["-af", SPEECHNORM_FILTER]
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
