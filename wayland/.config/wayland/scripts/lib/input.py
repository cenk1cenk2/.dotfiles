"""Input sources that provide the payload to process.

Most are passive — a clipboard, a pipe, a file already sitting on disk. The
microphone is the exception: it owns a live recorder and a lifecycle, and is
built by the adapter driving it rather than by `build_input`. It earns its
place here anyway, because what it ultimately hands over is the same thing
every other adapter hands over."""

import logging
import math
import struct
import subprocess
import sys
import threading
import time
from enum import StrEnum
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)


class InputMode(StrEnum):
    CLIPBOARD = "clipboard"
    STDIN = "stdin"
    FILE = "file"
    # Live capture. Not built by `build_input` — the recorder owns its own
    # lifecycle, so the adapter that drives it constructs it instead.
    MIC = "mic"



class InputAdapter(Protocol):
    mode: InputMode
    # Names the payload for backends that upload it; only the file
    # adapter knows a real one.
    name: str

    def read(self) -> str | None:
        """Return the text to process, or None on failure."""
        ...

    def read_bytes(self) -> bytes | None:
        """Return the raw payload, or None on failure."""
        ...


class InputAdapterClipboard:
    mode = InputMode.CLIPBOARD
    name = "clipboard"

    def read(self) -> str | None:
        cmd = ["wl-paste", "--no-newline"]
        log.debug("spawn: %s", " ".join(cmd))
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            log.error("wl-paste failed: %s", e)
            return None
        log.debug("wl-paste stderr: %s", result.stderr.strip())
        return result.stdout

    @staticmethod
    def list_mime_types() -> list[str]:
        """MIME types advertised for the current clipboard selection."""
        cmd = ["wl-paste", "--list-types"]
        log.debug("spawn: %s", " ".join(cmd))
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        except subprocess.CalledProcessError, FileNotFoundError:
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    @staticmethod
    def read_binary(mime: str) -> bytes | None:
        """Clipboard payload for `mime` as raw bytes."""
        cmd = ["wl-paste", "--no-newline", "--type", mime]
        log.debug("spawn: %s", " ".join(cmd))
        try:
            result = subprocess.run(cmd, capture_output=True, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            log.debug("wl-paste %s failed: %s", mime, e)
            return None
        return result.stdout or None

    def read_bytes(self) -> bytes | None:
        types = self.list_mime_types()
        mime = next((t for t in types if t.startswith("audio/")), None)
        if mime is None:
            log.error("clipboard holds no audio: %s", types)
            return None
        self.name = f"clipboard.{mime.removeprefix('audio/')}"

        return self.read_binary(mime)


class InputAdapterStdin:
    mode = InputMode.STDIN
    name = "stdin"

    def read(self) -> str | None:
        try:
            return sys.stdin.read()
        except Exception as e:
            log.error("stdin read failed: %s", e)
            return None

    def read_bytes(self) -> bytes | None:
        try:
            return sys.stdin.buffer.read()
        except Exception as e:
            log.error("stdin read failed: %s", e)
            return None


class InputAdapterFile:
    mode = InputMode.FILE

    def __init__(self, path: Path):
        self.path = path
        self.name = path.name

    def read(self) -> str | None:
        try:
            return self.path.read_text(encoding="utf-8")
        except OSError as e:
            log.error("read failed: %s", e)
            return None

    def read_bytes(self) -> bytes | None:
        try:
            return self.path.read_bytes()
        except OSError as e:
            log.error("read failed: %s", e)
            return None


def build_input(mode: InputMode, **kwargs) -> InputAdapter:
    """Adapter for `mode`, handed whatever that adapter takes.

    Unset knobs drop out, so a caller can pass every flag it has and let
    the mode decide. One that is set but wrong reaches the constructor
    and raises there rather than being silently ignored."""
    kwargs = {k: v for k, v in kwargs.items() if v is not None}
    try:
        match mode:
            case InputMode.CLIPBOARD:
                return InputAdapterClipboard(**kwargs)
            case InputMode.STDIN:
                return InputAdapterStdin(**kwargs)
            case InputMode.FILE:
                if kwargs.get("path") is None:
                    raise ValueError("file input requires a path")
                return InputAdapterFile(**kwargs)
            case _:
                raise ValueError(f"unsupported input mode: {mode!r}")
    except TypeError as e:
        raise ValueError(f"{mode.value} input takes no {', '.join(kwargs)}") from e


# The realtime socket's wire format; the batch path is resampled server-side
# anyway, so one capture rate serves both.
CAPTURE_RATE = 24000
SAMPLE_BYTES = 2

# Opus at this bitrate is roughly a tenth of the WAV it replaces and the
# endpoint decodes it natively. `voip` is the application profile tuned for
# speech rather than music.
OPUS_BITRATE = "24k"
OPUS_APPLICATION = "voip"
ENCODE_TIMEOUT = 30.0

# Bars the overlay draws, and how much recent audio each frame summarises.
BARS = 32
FRAME_SECONDS = 1 / 30


class MicCapture:
    """A live `pw-record` capture, readable as an upload once stopped.

    Satisfies `InputAdapter` (`mode`, `name`, `read_bytes`), so it can be
    handed straight to `SttAdapterHttp` in place of a file."""

    mode = InputMode.MIC
    name = "capture.opus"

    def __init__(self, rate: int = CAPTURE_RATE, target: str | None = None):
        self.rate = rate
        self.target = target
        self._proc: subprocess.Popen[bytes] | None = None
        self._reader: threading.Thread | None = None
        self._pcm = bytearray()
        self._lock = threading.Lock()
        self._started = 0.0

    # ── lifecycle ─────────────────────────────────────────────────

    def start(self) -> None:
        cmd = [
            "pw-record",
            # Without this `pw-record` writes a container header before the
            # samples, which corrupts both the Opus encode and the first
            # chunk pushed at the realtime socket.
            "--raw",
            "--rate",
            str(self.rate),
            "--channels",
            "1",
            "--format",
            "s16",
        ]
        if self.target:
            cmd += ["--target", self.target]
        cmd.append("-")

        log.debug("spawn: %s", " ".join(cmd))
        # Raw Popen rather than `dotlib.cli.run`: that helper starts children
        # in their own session, which would put the recorder out of reach of
        # the session's `killpg` and leave a microphone open after a kill.
        self._proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=sys.stderr
        )
        self._started = time.monotonic()
        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()

    def _drain(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        while chunk := self._proc.stdout.read(4096):
            with self._lock:
                self._pcm += chunk

    def is_recording(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def stop(self) -> None:
        """End the capture and wait for the recorder to actually be gone.

        Synchronous on purpose: the session plays a chime and lifts the
        playback ducking the moment this returns, and an asynchronous stop
        would let both bleed into the tail of the recording."""
        proc = self._proc
        if proc is None:
            return

        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                log.warning("pw-record ignored SIGTERM; killing")
                proc.kill()
                proc.wait()
        if self._reader is not None:
            self._reader.join(timeout=2)
        log.debug("captured %.1fs (%d bytes)", self.seconds, len(self._pcm))

    def cancel(self) -> None:
        self.stop()
        with self._lock:
            self._pcm.clear()

    # ── readouts ──────────────────────────────────────────────────

    @property
    def seconds(self) -> float:
        with self._lock:
            return len(self._pcm) / SAMPLE_BYTES / self.rate

    def frame(self) -> tuple[float, list[float]] | None:
        """Peak and `BARS` bucket levels over the most recent audio.

        None before anything has been captured, so a caller can poll this
        from the first tick without guarding on the recorder's state."""
        want = int(self.rate * FRAME_SECONDS) * SAMPLE_BYTES
        with self._lock:
            if not self._pcm:
                return None
            window = bytes(self._pcm[-want:])

        count = len(window) // SAMPLE_BYTES
        if count < BARS:
            return None

        samples = struct.unpack(f"<{count}h", window[: count * SAMPLE_BYTES])
        size = count // BARS
        bars = []
        for i in range(BARS):
            bucket = samples[i * size : (i + 1) * size]
            mean = sum(s * s for s in bucket) / len(bucket)
            bars.append(min(1.0, math.sqrt(mean) / 32768.0))

        return max(bars), bars

    def pcm(self) -> bytes:
        """Everything captured so far, as raw s16le."""
        with self._lock:
            return bytes(self._pcm)

    def read(self) -> str | None:
        raise TypeError("a microphone capture holds audio, not text")

    def read_bytes(self) -> bytes | None:
        """The capture as Ogg Opus, ready to upload."""
        raw = self.pcm()
        if not raw:
            log.error("capture held no audio")
            return None

        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "s16le",
            "-ar",
            str(self.rate),
            "-ac",
            "1",
            "-i",
            "pipe:0",
            "-c:a",
            "libopus",
            "-b:a",
            OPUS_BITRATE,
            "-application",
            OPUS_APPLICATION,
            "-f",
            "ogg",
            "pipe:1",
        ]
        log.debug("spawn: %s (%d bytes in)", " ".join(cmd), len(raw))
        try:
            proc = subprocess.run(
                cmd,
                input=raw,
                capture_output=True,
                timeout=ENCODE_TIMEOUT,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            log.error("opus encode failed: %s", e)
            return None

        if proc.returncode != 0:
            log.error("ffmpeg exit=%d: %s", proc.returncode, proc.stderr.decode())
            return None

        log.info(
            "encoded %.1fs to %d bytes of opus", self.seconds, len(proc.stdout)
        )

        return proc.stdout or None
