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
import signal
import subprocess
import sys
import threading
import time
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

    def pause(self) -> bool: ...

    def seek(self, seconds: float) -> bool: ...

    def cycle_tempo(self) -> float: ...


class PlayerBase:
    """The pump every sink shares, and the transport over the running player.

    Pause is a signal, but seek and speed both end in a *respawn*: a player
    buffers seconds ahead of the speakers, so a change made at the pipe is
    heard that much later, and ffplay's filter chain is fixed at startup
    besides. Raw PCM is what makes a respawn cheap - the samples carry no
    state, so a fresh player picks up mid-utterance wherever it is fed from.
    Everything handed to a player is kept for exactly that: it is the only
    copy of what has already been spoken, and a seek feeds it again.

    A control runs on the session's socket thread and does no more than
    record where it wants the player and kill the one that is up. The
    respawn happens on the pump thread, so the pipe keeps one writer."""

    TEMPOS = (1.0, 1.5)
    # How long to block on a finished player before looking for a seek.
    DRAIN_POLL_SECONDS = 0.1

    def __init__(self):
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._sent = bytearray()
        self._rate = DEFAULT_TTS_SAMPLE_RATE
        # Where the running player was started, and how much audio it has
        # played since. Split in two because a pause stops the clock.
        self._origin = 0
        self._played = 0.0
        self._since: float | None = None
        # Byte offset the pump should bring a player up at, once it looks.
        self._pending: int | None = None
        # Players a seek replaced, waiting for the pump to close and collect.
        self._spent: list[subprocess.Popen] = []
        self.paused = False
        self.tempo = 1.0

    # ── what a sink is ────────────────────────────────────────────

    def _command(self, sample_rate: int) -> list[str]:
        raise NotImplementedError(f"{type(self).__name__} declares no command")

    @property
    def seeks(self) -> bool:
        """Whether a byte offset into this sink's stream means a time."""
        return True

    @property
    def retimes(self) -> bool:
        """Whether this sink's command has a filter chain to hold `atempo`."""
        return False

    # ── transport, from the session's socket thread ───────────────

    def pause(self) -> bool:
        """Flip the pause; returns the state it landed in.

        SIGSTOP rather than holding the pump back: a writer that stops still
        leaves the player's buffer to come. A frozen player stops reading
        instead, the pipe fills, and the pump and the HTTP body behind it
        stall on their own.

        A pause taken before there is a player is held and applied at the
        next spawn - synthesis takes seconds, and a press inside that window
        means the utterance being prepared."""
        with self._lock:
            self.paused = not self.paused
            if self.paused:
                self._mark()
            else:
                self._since = time.monotonic()
            self._signal(signal.SIGSTOP if self.paused else signal.SIGCONT)

            return self.paused

    def seek(self, seconds: float) -> bool:
        """Scrub by `seconds`, negative to go back; False if the sink cannot.

        Forward is capped at what has been synthesized: the stream arrives as
        the backend writes it, so there is nothing past that to skip into."""
        if not self.seeks:
            return False

        with self._lock:
            self._restart(self._position + int(seconds * self._rate * 2))

            return True

    def cycle_tempo(self) -> float:
        """Step to the next playback rate; returns the one it landed on.

        Playback only - `TtsSpec.speed` is the rate the backend synthesizes at,
        which is fixed for the utterance by the time a player sees it."""
        if not self.retimes:
            return self.tempo

        with self._lock:
            at = self._position
            self.tempo = self.TEMPOS[
                (self.TEMPOS.index(self.tempo) + 1) % len(self.TEMPOS)
            ]
            self._restart(at)

            return self.tempo

    # ── clock and process, always under the lock ──────────────────

    @property
    def _position(self) -> int:
        """Byte offset of what is being heard now, at best estimate.

        A player consumes in real time, so the wall clock is the readout -
        none of them reports a position of its own. Clamped to what has been
        sent, which is where the estimate lands if the backend ever ran
        slower than playback and the player starved. Rounded to a sample,
        because half of an s16 frame shifts every sample after it into noise.

        A respawn already asked for wins over the clock: the player it will
        replace is dead, so its clock says where playback *was*, and a scrub
        held down out-paces the pump. Read that way, each step of a held key
        moves on from the last rather than all of them landing together."""
        if self._pending is not None:
            return self._pending

        running = (time.monotonic() - self._since) * self.tempo if self._since else 0.0
        offset = self._origin + int((self._played + running) * self._rate * 2)

        return max(0, min(offset, len(self._sent))) & ~1

    def _mark(self) -> None:
        """Fold the running segment into the played total, stopping the clock."""
        if self._since is not None:
            self._played += (time.monotonic() - self._since) * self.tempo
            self._since = None

    def _signal(self, sig: int) -> None:
        if self._proc is None:
            return
        try:
            self._proc.send_signal(sig)
        except ProcessLookupError:
            log.debug("player already gone")

    def _restart(self, at: int) -> None:
        """Post a respawn at byte `at` and drop the player that is up.

        Killing it is what wakes the pump out of its blocking write; SIGKILL
        reaches a stopped process, so a scrub works while paused."""
        self._pending = max(0, min(at, len(self._sent))) & ~1
        proc, self._proc = self._proc, None
        if proc is not None:
            proc.kill()
            self._spent.append(proc)

    # ── pump, from the run thread ─────────────────────────────────

    def _spawn(self, at: int) -> None:
        # Spawned under the lock, request included, so a seek arriving while
        # the process comes up cannot read the clock of the player being
        # replaced. Held down, a scrub steps from the last target rather than
        # every step landing on the same one.
        with self._lock:
            cmd = self._command(self._rate)
            log.debug("spawn at %d bytes: %s", at, " ".join(cmd))
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=sys.stderr,
                stderr=sys.stderr,
            )
            assert proc.stdin is not None
            self._proc = proc
            self._origin = at
            self._played = 0.0
            self._since = None if self.paused else time.monotonic()
            if self.paused:
                self._signal(signal.SIGSTOP)
            self._pending = None
            backlog = bytes(self._sent[at:])

        # Everything already spoken past the seek point, before the live
        # stream resumes. A paused player takes what fits in the pipe and
        # this blocks on the rest until it is let go, which is the point.
        if backlog:
            self._push(backlog)

    def _push(self, chunk: bytes) -> bool:
        """Write to the player; False once it is gone, respawned or dead."""
        proc = self._proc
        if proc is None or proc.stdin is None:
            return False

        try:
            proc.stdin.write(chunk)
        except BrokenPipeError:
            log.debug("player closed the pipe")
            with self._lock:
                if self._proc is proc:
                    self._proc = None
                self._spent.append(proc)
            return False

        return True

    def _reap(self) -> None:
        """Close and collect the players a seek replaced.

        On the pump thread, because it is the only one that writes to a
        player's pipe and so the only one that may close one. Left undone,
        the dropped stdin flushes into a dead pipe when Python finalizes it,
        and the process stays a zombie for the length of the run."""
        with self._lock:
            spent, self._spent = self._spent, []
        for proc in spent:
            if proc.stdin is not None:
                try:
                    proc.stdin.close()
                except BrokenPipeError:
                    pass
            proc.wait()

    def _close(self) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            return
        try:
            proc.stdin.close()
        except BrokenPipeError:
            pass

    def play(self, stream: ByteStream, sample_rate: int) -> tuple[int, int]:
        """Drain `stream` into the sink; returns (bytes received, exit code).

        The count is what the backend delivered rather than what reached a
        player: a seek feeds the same samples twice, and the caller asks this
        to find out whether any audio arrived at all.

        The player deliberately stays in our process group so the session's
        `killpg` reaches it. Only the last player's exit code is reported -
        every one before it was killed on purpose, to be replaced."""
        with self._lock:
            self._sent = bytearray()
            self._rate = sample_rate
            self._origin = 0
            self._played = 0.0
            self._since = None
            # `tempo` deliberately survives: a rate set to skim a backlog was
            # meant for the backlog, not for the one item it was pressed on.
            # Brings the first player up on the opening pass.
            self._pending = 0

        code = 0
        draining = False
        while True:
            self._reap()
            with self._lock:
                pending = self._pending
            if pending is not None:
                self._spawn(pending)
                if draining:
                    self._close()
                # Around again rather than on: a control pressed while the
                # backlog was being written has already killed this player and
                # posted the next request, and reading `_proc` now would find
                # the hole it left and take it for a dead player.
                continue
            proc = self._proc
            if proc is None:
                break

            if not draining:
                chunk = stream.read(CHUNK_BYTES)
                if chunk:
                    with self._lock:
                        self._sent += chunk
                    # A failed write is a player that went away; the loop
                    # respawns it and the backlog carries this chunk with it.
                    self._push(chunk)
                    continue

                draining = True
                self._close()

            # Nothing left to feed. Waiting in slices rather than once, so a
            # seek during the tail - the common case, since the backend runs
            # ahead of the speakers - is still answered.
            try:
                code = proc.wait(timeout=self.DRAIN_POLL_SECONDS)
            except subprocess.TimeoutExpired:
                continue
            with self._lock:
                if self._proc is proc:
                    self._proc = None
                pending = self._pending
            if pending is None:
                break

        self._reap()
        with self._lock:
            self.paused = False
            self._since = None

        return len(self._sent), code


class PlayerAdapterFfplay(PlayerBase):
    """ffmpeg's player — the only sink that can demux a container.

    Also the only one that can normalise or retime: both are ffmpeg filters,
    and the raw sinks below take a linear volume at best."""

    mode = PlayerMode.FFPLAY

    def __init__(
        self,
        response_format: AudioFormat = AudioFormat.PCM,
        normalize: bool = True,
    ):
        super().__init__()
        self.response_format = response_format
        self.normalize = normalize

    @property
    def seeks(self) -> bool:
        # A container's bytes are not samples, so no offset into one names a
        # time, and the position the transport works in has no meaning.
        return self.response_format is AudioFormat.PCM

    @property
    def retimes(self) -> bool:
        return self.seeks

    def _command(self, sample_rate: int) -> list[str]:
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
        # `atempo` first: it changes the length of what follows, and the
        # normaliser should read the samples as they will be heard.
        filters = [f"atempo={self.tempo}"] if self.tempo != 1.0 else []
        if self.normalize:
            filters.append(SPEECHNORM_FILTER)
        if filters:
            cmd += ["-af", ",".join(filters)]
        cmd += ["-i", "pipe:0"]

        return cmd


class PlayerAdapterPwCat(PlayerBase):
    """PipeWire's own sink. Raw s16le only."""

    mode = PlayerMode.PW_CAT

    def _command(self, sample_rate: int) -> list[str]:
        return [
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


class PlayerAdapterPaplay(PlayerBase):
    """PulseAudio compatibility sink. Raw s16le only.

    No file argument: `paplay` is libpulse's `pacat`, which reads stdin
    only when none is given and opens a literal `-` as a filename."""

    mode = PlayerMode.PAPLAY

    def _command(self, sample_rate: int) -> list[str]:
        return [
            "paplay",
            "--raw",
            "--format=s16le",
            f"--rate={sample_rate}",
            "--channels=1",
        ]


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
