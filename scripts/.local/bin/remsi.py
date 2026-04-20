#!/usr/bin/env -S sh -c 'exec uv run --project "$(dirname "$0")" "$0" "$@"'
"""Remove silent + filler regions from video via ffmpeg.

Same shebang pattern as the wayland scripts — `sh -c` trampoline
so `uv run --project <this-dir>` resolves regardless of the
shell's cwd. The `.py` extension is load-bearing: without it, uv
re-interprets the shebang on invocation and recurses."""

from __future__ import annotations

import bisect
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterator, Optional, Protocol

import click
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

# ── Console + logging ────────────────────────────────────────────────
#
# Two rich consoles, two audiences:
#   - `console` (stdout): user-facing output — section rules,
#     summary rows, before/after tables. This is what the operator
#     reads. `remsi` is run interactively (never piped), so writing
#     the pretty output to stdout is fine.
#   - RichHandler-backed logger (stderr): traceable events — every
#     subprocess spawn, per-region debug, per-step start/end.
#     Timestamped + level-tagged, scannable under `-v`.

console: Console = Console(force_terminal=None)
_log_console: Console = Console(file=sys.stderr, stderr=True, force_terminal=None)


def create_logger(verbose: bool) -> logging.Logger:
    """Install a rich handler on the root logger, bound to stderr."""
    root = logging.getLogger()
    level = logging.DEBUG if verbose else logging.INFO
    root.setLevel(level)
    if not any(isinstance(h, RichHandler) for h in root.handlers):
        for h in list(root.handlers):
            root.removeHandler(h)
        handler = RichHandler(
            console=_log_console,
            show_path=False,
            show_time=True,
            rich_tracebacks=True,
            markup=True,
            log_time_format="[%H:%M:%S]",
        )
        handler.setLevel(level)
        root.addHandler(handler)
    else:
        for h in root.handlers:
            h.setLevel(level)
    return logging.getLogger("remsi")


log = logging.getLogger("remsi")

# ── Domain types ─────────────────────────────────────────────────────

class RegionKind(StrEnum):
    SILENCE = "silence"
    FILLER = "filler"
    STUTTER = "stutter"
    GAP = "gap"
    SPEECH = "speech"

    @property
    def priority(self) -> int:
        return {
            self.SILENCE: 2,
            self.FILLER: 1,
            self.STUTTER: 1,
            self.GAP: 0,
            self.SPEECH: -1,
        }[self]

    @property
    def style(self) -> str:
        """Rich-markup colour for this kind in summary listings."""
        return {
            self.SILENCE: "yellow",
            self.FILLER: "red",
            self.STUTTER: "blue",
            self.GAP: "dim",
            self.SPEECH: "green",
        }[self]

@dataclass
class Region:
    start: float
    end: float
    kind: RegionKind

@dataclass
class Segment:
    start: float
    end: float
    left: Region | None = field(default=None, repr=False)
    right: Region | None = field(default=None, repr=False)

@dataclass
class TimedWord:
    text: str
    start: float
    end: float

@dataclass
class VideoInfo:
    codec: str | None = None
    bitrate: int | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    pix_fmt: str | None = None
    color_space: str | None = None
    color_transfer: str | None = None
    color_primaries: str | None = None
    color_range: str | None = None

    def __str__(self) -> str:
        parts: list[str] = []
        if self.codec:
            parts.append(self.codec)
        if self.width and self.height:
            parts.append(f"{self.width}x{self.height}")
        if self.fps:
            parts.append(f"{self.fps:g}fps")
        if self.pix_fmt:
            parts.append(self.pix_fmt)
        if self.color_space and self.color_space != "unknown":
            parts.append(self.color_space)
        if self.color_range and self.color_range != "unknown":
            parts.append(self.color_range)
        if self.bitrate:
            parts.append(f"{self.bitrate // 1000}k")
        return " ".join(parts) or "?"

@dataclass
class AudioInfo:
    codec: str | None = None
    bitrate: int | None = None
    sample_rate: int | None = None
    channels: int | None = None

    def __str__(self) -> str:
        parts: list[str] = []
        if self.codec:
            parts.append(self.codec)
        if self.sample_rate:
            parts.append(f"{self.sample_rate // 1000}kHz")
        if self.channels:
            parts.append(f"{self.channels}ch")
        if self.bitrate:
            parts.append(f"{self.bitrate // 1000}k")
        return " ".join(parts) or "?"

@dataclass
class MediaInfo:
    video: VideoInfo = field(default_factory=VideoInfo)
    audio: AudioInfo = field(default_factory=AudioInfo)
    size: int | None = None

    @property
    def size_str(self) -> str:
        if self.size is None:
            return "?"
        if self.size >= 1_073_741_824:
            return f"{self.size / 1_073_741_824:.2f} GB"
        if self.size >= 1_048_576:
            return f"{self.size / 1_048_576:.1f} MB"
        if self.size >= 1024:
            return f"{self.size / 1024:.1f} KB"
        return f"{self.size} B"

def format_timestamp(seconds: float) -> str:
    s = float(seconds)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"

FILLER_PATTERN = re.compile(
    r"^("
    r"u+[hm]+|"  # uh, uhh, um, umm, uhm
    r"[hm]+m*|"  # hm, hmm, mm, mmm, mhm
    r"e+r+m*|"  # er, erm, errr
    r"a+h*|"  # ah, ahh, aaa, aaah
    r"o+h+|"  # oh, ohh
    r"e+h+|"  # eh, ehh
    r")$"
)

# ── Transcription adapters ───────────────────────────────────────────

def _extract_wav(input_file: Path, output_path: str) -> None:
    cmd = [
        "ffmpeg",
        "-i",
        str(input_file),
        "-ar",
        "16000",
        "-ac",
        "1",
        "-f",
        "wav",
        "-y",
        output_path,
    ]
    log.info("spawn: %s", " ".join(cmd))
    result = subprocess.run(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        log.error("ffmpeg stderr:\n%s", result.stderr)
        raise RuntimeError("failed to extract audio for transcription")

class TranscriptionProvider(StrEnum):
    WHISPER_CPP = "whisper-cpp"
    HTTP = "http"

class TranscriptionAdapter(Protocol):
    """Turns an audio/video file into word-level timestamps."""

    name: str

    def transcribe(self, input_file: Path) -> list[TimedWord]: ...

class TranscriptionAdapterWhisperCpp:
    """Local whisper.cpp via the `whisper-cli` binary."""

    name = "whisper-cpp"

    def __init__(self, model_path: Path):
        self.model_path = model_path

    def transcribe(self, input_file: Path) -> list[TimedWord]:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as tmp:
            _extract_wav(input_file, tmp.name)
            cmd = [
                "whisper-cli",
                "-m",
                str(self.model_path),
                "-pp",
                "--max-len",
                "1",
                "--split-on-word",
                "-oj",
                "-of",
                tmp.name,
                tmp.name,
            ]
            log.info("spawn: %s", " ".join(cmd))
            result = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=sys.stderr,
            )
            if result.returncode != 0:
                raise RuntimeError(f"whisper-cli failed (exit {result.returncode})")

            json_path = f"{tmp.name}.json"
            try:
                with open(json_path) as f:
                    data = json.load(f)
            finally:
                Path(json_path).unlink(missing_ok=True)

        return [
            TimedWord(
                text=seg["text"],
                start=seg["offsets"]["from"] / 1000,
                end=seg["offsets"]["to"] / 1000,
            )
            for seg in data.get("transcription", [])
        ]

class TranscriptionAdapterHttp:
    """OpenAI-compatible `/audio/transcriptions` endpoint returning
    `verbose_json` with word-level timestamps."""

    name = "http"

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str,
        user_agent: str = "remsi/1.0",
    ):
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self.user_agent = user_agent

    def transcribe(self, input_file: Path) -> list[TimedWord]:
        import requests

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as tmp:
            _extract_wav(input_file, tmp.name)
            url = f"{self.base_url}/audio/transcriptions"
            log.info("POST %s model=%s", url, self.model)
            try:
                with open(tmp.name, "rb") as f:
                    response = requests.post(
                        url,
                        headers={
                            "Authorization": f"Bearer {self.api_key}",
                            "User-Agent": self.user_agent,
                        },
                        files={"file": ("audio.wav", f, "audio/wav")},
                        data={
                            "model": self.model,
                            "response_format": "verbose_json",
                            "timestamp_granularities[]": ["segment", "word"],
                        },
                    )
            except requests.RequestException as e:
                raise RuntimeError(f"HTTP STT request failed: {e}") from e

        if not response.ok:
            raise RuntimeError(
                f"HTTP STT failed: {response.status_code} {response.text}"
            )
        data = response.json()
        log.debug("HTTP STT response keys: %s", list(data.keys()))

        words = data.get("words", [])
        if words:
            return [
                TimedWord(text=w["word"], start=w["start"], end=w["end"]) for w in words
            ]

        segments = data.get("segments", [])
        if segments:
            log.debug("no word-level timestamps; falling back to segments")
            result: list[TimedWord] = []
            for seg in segments:
                seg_words = seg.get("words", [])
                if seg_words:
                    for w in seg_words:
                        result.append(
                            TimedWord(text=w["word"], start=w["start"], end=w["end"])
                        )
                else:
                    result.append(
                        TimedWord(
                            text=seg.get("text", ""),
                            start=seg["start"],
                            end=seg["end"],
                        )
                    )
            return result

        log.warning("HTTP STT returned no words or segments")
        return []

# ── Analyzer ─────────────────────────────────────────────────────────

class Analyzer:
    def __init__(
        self,
        noise: str,
        duration: float,
        stt_adapter: Optional[TranscriptionAdapter] = None,
    ):
        self.noise = noise
        self.duration = duration
        self.stt_adapter = stt_adapter

    @staticmethod
    def get_duration(input_file: Path) -> Optional[float]:
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(input_file),
        ]
        log.debug("spawn: %s", " ".join(cmd))
        result = subprocess.run(cmd, capture_output=True, text=True)
        try:
            return float(result.stdout.strip())
        except ValueError:
            log.debug("ffprobe stderr: %s", result.stderr.strip())
            return None

    def detect_silence(self, input_file: Path, total: float) -> list[Region]:
        cmd = [
            "ffmpeg",
            "-i",
            str(input_file),
            "-hide_banner",
            "-af",
            f"silencedetect=n={self.noise}:d={self.duration}",
            "-f",
            "null",
            "-",
        ]
        log.info("spawn: %s", " ".join(cmd))
        proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True
        )
        assert proc.stderr is not None
        lines: list[str] = []
        for line in proc.stderr:
            sys.stderr.write(line)
            lines.append(line)
        proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(f"silence detection failed (exit {proc.returncode})")

        silences: list[Region] = []
        silence_start: Optional[float] = None
        for line in "".join(lines).splitlines():
            start_match = re.search(r"silence_start: (\d+\.?\d+)", line)
            end_match = re.search(r"silence_end: (\d+\.?\d+)", line)
            if start_match:
                silence_start = float(start_match.group(1))
            if end_match and silence_start is not None:
                silences.append(
                    Region(silence_start, float(end_match.group(1)), RegionKind.SILENCE)
                )
                silence_start = None
        if silence_start is not None:
            silences.append(Region(silence_start, total, RegionKind.SILENCE))
        return silences

    @staticmethod
    def _classify_words(
        words: list[TimedWord],
    ) -> tuple[list[Region], list[Region], list[Region]]:
        speech: list[Region] = []
        fillers: list[Region] = []
        stutters: list[Region] = []
        prev_letters: Optional[str] = None
        for w in words:
            text = w.text.strip()
            letters = re.sub(r"[^a-z]", "", text.lower())
            if w.start >= w.end:
                continue
            is_filler = (not text) or bool(letters and FILLER_PATTERN.match(letters))
            if is_filler:
                fillers.append(Region(w.start, w.end, RegionKind.FILLER))
                log.debug(
                    "filler: %r %s → %s",
                    text,
                    format_timestamp(w.start),
                    format_timestamp(w.end),
                )
                prev_letters = None
            elif letters and letters == prev_letters:
                stutters.append(Region(w.start, w.end, RegionKind.STUTTER))
                log.debug(
                    "stutter: %r %s → %s",
                    text,
                    format_timestamp(w.start),
                    format_timestamp(w.end),
                )
            else:
                speech.append(Region(w.start, w.end, RegionKind.SPEECH))
                prev_letters = letters if letters else None
        return speech, fillers, stutters

    @staticmethod
    def _find_uncovered_gaps(known_regions: list[Region]) -> list[Region]:
        known_regions.sort(key=lambda r: r.start)
        merged: list[Region] = []
        for r in known_regions:
            if merged and r.start <= merged[-1].end:
                merged[-1] = Region(
                    merged[-1].start, max(merged[-1].end, r.end), merged[-1].kind
                )
            else:
                merged.append(r)

        gaps: list[Region] = []
        pos = 0.0
        for r in merged:
            if r.start > pos:
                gaps.append(Region(pos, r.start, RegionKind.GAP))
            pos = r.end
        return gaps

    def detect_filler_words(
        self, input_file: Path, silences: list[Region]
    ) -> list[Region]:
        if self.stt_adapter is None:
            return []
        words = self.stt_adapter.transcribe(input_file)
        speech, fillers, stutters = self._classify_words(words)
        gaps = self._find_uncovered_gaps(silences + speech + fillers + stutters)
        regions = fillers + gaps + stutters
        for i, r in enumerate(regions, 1):
            log.info(
                "%d. %s → %s (%.1fs) [%s]%s[/]",
                i,
                format_timestamp(r.start),
                format_timestamp(r.end),
                r.end - r.start,
                r.kind.style,
                r.kind,
            )
        log.info(
            "speech: %d · fillers: %d · stutters: %d · gaps: %d",
            len(speech),
            len(fillers),
            len(stutters),
            len(gaps),
        )
        return regions

    @staticmethod
    def merge_regions(silences: list[Region], fillers: list[Region]) -> list[Region]:
        regions = list(silences) + list(fillers)
        regions.sort(key=lambda r: r.start)
        if not regions:
            return []
        merged = [regions[0]]
        for r in regions[1:]:
            prev = merged[-1]
            if r.start <= prev.end:
                stronger = (
                    prev.kind if prev.kind.priority >= r.kind.priority else r.kind
                )
                merged[-1] = Region(prev.start, max(prev.end, r.end), stronger)
            else:
                merged.append(r)
        return merged

    @staticmethod
    def regions_to_segments(regions: list[Region], total: float) -> list[Segment]:
        segments: list[Segment] = []
        pos = 0.0
        for region in regions:
            if region.start > pos:
                left = segments[-1].right if segments else None
                segments.append(
                    Segment(start=pos, end=region.start, left=left, right=region)
                )
            pos = region.end
        if pos < total:
            left = segments[-1].right if segments else None
            segments.append(Segment(start=pos, end=total, left=left))
        return segments

# ── Encoder adapters ─────────────────────────────────────────────────

class EncoderKind(StrEnum):
    """Dispatch token for the `--encoder` flag.

    * `cut`       — pure concat-demuxer stream-copy, plus a single
                    re-encode pass for the audio track so stitch
                    points get a proper afade. Video is bit-for-bit
                    the source; cuts snap to keyframes.
    * `smart-cut` — keyframe-aware stream-copy for the middle of
                    each segment, re-encode the GOP lead-in /
                    lead-out. Frame-accurate at the cost of some
                    re-encoding.
    * `fancy`     — full filter_complex with xfade / acrossfade.
                    One ffmpeg pass, nicest transitions, slowest.
    """

    CUT = "cut"
    SMART_CUT = "smart-cut"
    FANCY = "fancy"

class Encoder:
    GPU_ENCODERS = {
        "h264": {"nvidia": "h264_nvenc", "amd": "h264_amf", "vaapi": "h264_vaapi"},
        "hevc": {"nvidia": "hevc_nvenc", "amd": "hevc_amf", "vaapi": "hevc_vaapi"},
        "av1": {"nvidia": "av1_nvenc", "amd": "av1_amf", "vaapi": "av1_vaapi"},
    }

    def __init__(self, gpu: Optional[str], codec: Optional[str], force: bool = False):
        self.gpu = gpu
        self.codec = codec
        self.force = force

    @staticmethod
    def detect_gpu() -> Optional[str]:
        cmd = ["ffmpeg", "-hide_banner", "-encoders"]
        log.debug("spawn: %s", " ".join(cmd))
        encoders = subprocess.run(cmd, capture_output=True, text=True).stdout
        if "hevc_nvenc" in encoders:
            return "nvidia"
        if "hevc_amf" in encoders:
            return "amd"
        if "hevc_vaapi" in encoders:
            return "vaapi"
        return None

    @staticmethod
    def _ffprobe_stream(
        input_file: Path, stream_type: str, fields: list[str]
    ) -> dict[str, Any]:
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            f"{stream_type}:0",
            "-show_entries",
            f"stream={','.join(fields)}",
            "-of",
            "json",
            str(input_file),
        ]
        log.debug("spawn: %s", " ".join(cmd))
        result = subprocess.run(cmd, capture_output=True, text=True)
        try:
            streams = json.loads(result.stdout).get("streams", [])
            if streams:
                return streams[0]
        except json.JSONDecodeError, IndexError:
            log.debug("ffprobe parse failed: %s", result.stderr.strip())
        return {}

    @staticmethod
    def _parse_fps(r_frame_rate: Optional[str]) -> Optional[float]:
        try:
            num, den = (r_frame_rate or "").split("/")
            return round(int(num) / int(den), 3)
        except ValueError, ZeroDivisionError, AttributeError:
            return None

    @classmethod
    def probe(cls, input_file: Path) -> MediaInfo:
        vs = cls._ffprobe_stream(
            input_file,
            "v",
            [
                "codec_name",
                "bit_rate",
                "width",
                "height",
                "r_frame_rate",
                "pix_fmt",
                "color_space",
                "color_transfer",
                "color_primaries",
                "color_range",
            ],
        )
        a = cls._ffprobe_stream(
            input_file, "a", ["codec_name", "bit_rate", "sample_rate", "channels"]
        )

        def _int(v: Any) -> Optional[int]:
            try:
                return int(v) if v else None
            except ValueError, TypeError:
                return None

        try:
            size = Path(input_file).stat().st_size
        except OSError:
            size = None

        return MediaInfo(
            size=size,
            video=VideoInfo(
                codec=vs.get("codec_name"),
                bitrate=_int(vs.get("bit_rate")),
                width=_int(vs.get("width")),
                height=_int(vs.get("height")),
                fps=cls._parse_fps(vs.get("r_frame_rate")),
                pix_fmt=vs.get("pix_fmt"),
                color_space=vs.get("color_space"),
                color_transfer=vs.get("color_transfer"),
                color_primaries=vs.get("color_primaries"),
                color_range=vs.get("color_range"),
            ),
            audio=AudioInfo(
                codec=a.get("codec_name"),
                bitrate=_int(a.get("bit_rate")),
                sample_rate=_int(a.get("sample_rate")),
                channels=_int(a.get("channels")),
            ),
        )

    def _video_codec_args(self, info: VideoInfo) -> list[str]:
        if self.codec:
            return ["-c:v", self.codec]

        family: Optional[str] = None
        if info.codec:
            match info.codec.lower():
                case "h264" | "libx264":
                    family = "h264"
                case "hevc" | "h265" | "libx265":
                    family = "hevc"
                case "av1" | "libsvtav1" | "libaom-av1":
                    family = "av1"

        args: list[str] = []
        if self.gpu and family and family in self.GPU_ENCODERS:
            hw_enc = self.GPU_ENCODERS[family].get(self.gpu)
            if hw_enc:
                match self.gpu:
                    case "vaapi":
                        args = [
                            "-vaapi_device",
                            "/dev/dri/renderD128",
                            "-c:v",
                            hw_enc,
                            "-qp",
                            "20",
                        ]
                    case "nvidia":
                        args = [
                            "-c:v",
                            hw_enc,
                            "-preset",
                            "p5",
                            "-tune",
                            "hq",
                            "-rc",
                            "constqp",
                            "-qp",
                            "20",
                            "-multipass",
                            "qres",
                            "-bf",
                            "2",
                        ]
                    case _:
                        args = ["-c:v", hw_enc, "-qp", "20"]
        elif info.codec:
            args = ["-c:v", info.codec, "-crf", "20"]

        if not args:
            return []
        if info.pix_fmt:
            args.extend(["-pix_fmt", info.pix_fmt])
        if info.color_space and info.color_space != "unknown":
            args.extend(["-colorspace", info.color_space])
        if info.color_transfer and info.color_transfer != "unknown":
            args.extend(["-color_trc", info.color_transfer])
        if info.color_primaries and info.color_primaries != "unknown":
            args.extend(["-color_primaries", info.color_primaries])
        if info.color_range and info.color_range != "unknown":
            match info.color_range:
                case "tv":
                    cr = "mpeg"
                case "pc":
                    cr = "jpeg"
                case _:
                    cr = info.color_range
            args.extend(["-color_range", cr])
        return args

    def _audio_codec_args(self, info: AudioInfo) -> list[str]:
        args = ["-c:a", info.codec or "aac"]
        if info.bitrate:
            args.extend(["-b:a", str(info.bitrate)])
        return args

    @staticmethod
    def _run(cmd: list[str]) -> subprocess.CompletedProcess:
        log.info("spawn: %s", " ".join(cmd))
        return subprocess.run(cmd)

    def encode(
        self,
        input_file: Path,
        output_file: Path,
        segments: list[Segment],
        media: Optional[MediaInfo] = None,
    ) -> subprocess.CompletedProcess:
        media = media or MediaInfo()
        if len(segments) == 1:
            seg = segments[0]
            cmd = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "warning",
                "-stats",
                "-ss",
                str(seg.start),
                "-to",
                str(seg.end),
                "-i",
                str(input_file),
                "-c:v",
                "copy",
                "-c:a",
                "copy",
            ]
            if self.force:
                cmd.append("-y")
            cmd.append(str(output_file))
            return self._run(cmd)
        return self._encode_segments(input_file, output_file, segments, media)

    def _encode_segments(
        self,
        input_file: Path,
        output_file: Path,
        segments: list[Segment],
        media: MediaInfo,
    ) -> subprocess.CompletedProcess:
        raise NotImplementedError

class FancyEncoder(Encoder):
    """filter_complex xfade/acrossfade path. Single ffmpeg pass."""

    def __init__(
        self,
        gpu: Optional[str],
        codec: Optional[str],
        fade_time: float,
        video_filter: str,
        audio_filter: str,
        force: bool = False,
    ):
        super().__init__(gpu, codec, force)
        self.fade_time = fade_time
        self.video_filter = video_filter
        self.audio_filter = audio_filter

    def _xfade_expr(self, offset: float) -> str:
        name, _, extra = self.video_filter.partition(":")
        parts = [f"{name}=offset={offset}:duration={self.fade_time}"]
        if extra:
            parts.append(extra)
        return ":".join(parts)

    def _acrossfade_expr(self) -> str:
        name, _, extra = self.audio_filter.partition(":")
        parts = [f"{name}=duration={self.fade_time}"]
        if extra:
            parts.append(extra)
        return ":".join(parts)

    @staticmethod
    @contextmanager
    def _filter_script(lines: list[str]) -> Iterator[str]:
        content = ";\n".join(lines)
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", prefix="remsi_filter_", delete=True
        ) as f:
            f.write(content)
            f.flush()
            log.debug("filter_complex_script %s:\n%s", f.name, content)
            yield f.name

    def _should_crossfade(self, i: int, segments: list[Segment]) -> bool:
        return (
            self.fade_time > 0
            and i < len(segments) - 1
            and segments[i].right is not None
            and segments[i].right.kind == RegionKind.SILENCE
            and (segments[i].end - segments[i].start) >= self.fade_time
            and (segments[i + 1].end - segments[i + 1].start) >= self.fade_time
        )

    def _build_filter_lines(
        self, segments: list[Segment], video_info: Optional[VideoInfo] = None
    ) -> list[str]:
        n = len(segments)
        color_filters = ""
        if video_info:
            parts: list[str] = []
            if video_info.pix_fmt:
                parts.append(f"format={video_info.pix_fmt}")
            sp: list[str] = []
            if video_info.color_space and video_info.color_space != "unknown":
                sp.append(f"colorspace={video_info.color_space}")
            if video_info.color_transfer and video_info.color_transfer != "unknown":
                sp.append(f"color_trc={video_info.color_transfer}")
            if video_info.color_primaries and video_info.color_primaries != "unknown":
                sp.append(f"color_primaries={video_info.color_primaries}")
            if video_info.color_range and video_info.color_range != "unknown":
                match video_info.color_range:
                    case "tv":
                        r = "limited"
                    case "pc":
                        r = "full"
                    case _:
                        r = video_info.color_range
                sp.append(f"range={r}")
            if sp:
                parts.append("setparams=" + ":".join(sp))
            if parts:
                color_filters = "," + ",".join(parts)

        lines: list[str] = []
        for i, seg in enumerate(segments):
            lines.append(
                f"[0:v]trim={seg.start}:{seg.end},setpts=PTS-STARTPTS,settb=AVTB{color_filters}[v{i}]"
            )
            lines.append(f"[0:a]atrim={seg.start}:{seg.end},asetpts=PTS-STARTPTS[a{i}]")

        if self.fade_time > 0 and n > 1:
            accumulated = 0.0
            xfade_count = 0
            v_label = "[v0]"
            for i in range(n - 1):
                accumulated += segments[i].end - segments[i].start
                if self._should_crossfade(i, segments):
                    offset = accumulated - (xfade_count + 1) * self.fade_time
                    out = f"[xf{xfade_count}]"
                    lines.append(f"{v_label}[v{i + 1}]{self._xfade_expr(offset)}{out}")
                    v_label = out
                    xfade_count += 1
                else:
                    out = f"[vc{i}]"
                    lines.append(f"{v_label}[v{i + 1}]concat=n=2:v=1:a=0{out}")
                    v_label = out
            lines.append(f"{v_label}null[vout]")
        else:
            vf_labels = "".join(f"[v{i}]" for i in range(n))
            lines.append(f"{vf_labels}concat=n={n}:v=1:a=0[vout]")

        if self.fade_time > 0 and n > 1:
            a_label = "[a0]"
            for i in range(n - 1):
                if self._should_crossfade(i, segments):
                    out = f"[ax{i}]"
                    lines.append(f"{a_label}[a{i + 1}]{self._acrossfade_expr()}{out}")
                    a_label = out
                else:
                    out = f"[ac{i}]"
                    lines.append(f"{a_label}[a{i + 1}]concat=n=2:v=0:a=1{out}")
                    a_label = out
            lines.append(f"{a_label}anull[aout]")
        else:
            af_labels = "".join(f"[a{i}]" for i in range(n))
            lines.append(f"{af_labels}concat=n={n}:v=0:a=1[aout]")
        return lines

    def _encode_segments(
        self,
        input_file: Path,
        output_file: Path,
        segments: list[Segment],
        media: MediaInfo,
    ) -> subprocess.CompletedProcess:
        lines = self._build_filter_lines(segments, media.video)
        with self._filter_script(lines) as script_path:
            cmd = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "warning",
                "-stats",
                "-i",
                str(input_file),
                "-/filter_complex",
                script_path,
                "-map",
                "[vout]",
                "-map",
                "[aout]",
            ]
            if self.force:
                cmd.append("-y")
            cmd.extend(self._video_codec_args(media.video))
            cmd.extend(self._audio_codec_args(media.audio))
            cmd.append(str(output_file))
            return self._run(cmd)

class CutEncoder(Encoder):
    """Stream-copy video + fade audio. Simpler cousin of
    `SmartCutEncoder`: no video re-encode anywhere.

    Two ffmpeg calls per input:
      1. Stream-copy each (keyframe-snapped) segment via the concat
         demuxer into a video-only intermediate.
      2. Re-encode the audio track in one filter_complex pass —
         `atrim` + `asetpts` per segment, `afade` at every internal
         boundary, `concat` the lot. Mux the result with the video
         from step 1 and the source stays bit-for-bit identical on
         the video side.

    Tradeoffs vs the other encoders:
      * Video is keyframe-snapped OUTWARD: the kept content is
        always fully preserved, but up to one GOP of silence may
        survive at each cut boundary. Shorter source GOPs = more
        cut precision. Two adjacent segments whose outward snaps
        overlap merge into one copy range.
      * Audio cuts are frame-accurate with real fades; no clicks.
      * Much faster than `fancy` (video not touched) and gentler
        than `smart-cut` (no GOP-edge re-encode)."""

    def __init__(
        self,
        gpu: Optional[str],
        codec: Optional[str],
        fade_time: float,
        force: bool = False,
    ):
        super().__init__(gpu, codec, force)
        self.fade_time = fade_time

    @staticmethod
    def _probe_keyframes(input_file: Path) -> list[float]:
        cmd = [
            "ffprobe",
            "-v", "error",
            "-select_streams", "v:0",
            "-skip_frame", "nokey",
            "-show_entries", "frame=pts_time",
            "-of", "csv=p=0",
            str(input_file),
        ]
        log.debug("spawn: %s", " ".join(cmd))
        result = subprocess.run(cmd, capture_output=True, text=True)
        keyframes: list[float] = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                keyframes.append(float(line))
            except ValueError:
                continue
        keyframes.sort()
        log.info("probed %d keyframes", len(keyframes))
        return keyframes

    @staticmethod
    def _snap_outward(
        segments: list[Segment], keyframes: list[float], total: float
    ) -> list[tuple[float, float]]:
        """Expand each segment's `[start, end]` outward to the nearest
        enclosing keyframes (`kf_before <= start`, `kf_after > end`),
        clamp to `[0, total]`, then merge overlaps.

        Overlap merges = silences too short to land on a GOP
        boundary. They stay in the output (we can't cut them
        losslessly); the merger surfaces that as one bigger copy
        range rather than two glued-together ones with a bad seam."""
        if not keyframes:
            return [(seg.start, seg.end) for seg in segments]

        expanded: list[tuple[float, float]] = []
        for seg in segments:
            idx_before = bisect.bisect_right(keyframes, seg.start) - 1
            kf_before = keyframes[idx_before] if idx_before >= 0 else 0.0
            idx_after = bisect.bisect_right(keyframes, seg.end)
            kf_after = keyframes[idx_after] if idx_after < len(keyframes) else total
            expanded.append((max(0.0, kf_before), min(total, kf_after)))

        expanded.sort(key=lambda r: r[0])
        merged: list[tuple[float, float]] = [expanded[0]]
        for a, b in expanded[1:]:
            pa, pb = merged[-1]
            if a <= pb:
                merged[-1] = (pa, max(pb, b))
            else:
                merged.append((a, b))
        return merged

    def _copy_video_part(
        self, input_file: Path, output_part: Path, start: float, end: float
    ) -> subprocess.CompletedProcess:
        """`-ss X -i input -to Y` stream-copies packets in the input
        timeline range `[X, Y]`. `-ss` before `-i` is fast container
        seek; both X and Y are keyframes in our case so the seek is
        exact. `-an` drops audio — we'll render it separately."""
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "warning",
            "-ss", str(start),
            "-to", str(end),
            "-i", str(input_file),
            "-c", "copy",
            "-an",
            "-avoid_negative_ts", "make_zero",
            "-y", str(output_part),
        ]
        return self._run(cmd)

    def _build_audio_filter(
        self, ranges: list[tuple[float, float]]
    ) -> tuple[str, list[str]]:
        """Build the audio-only filter_complex that atrims each
        range, afades at every internal boundary, and concats the
        lot. Returns (script_content, output_label_args)."""
        lines: list[str] = []
        for i, (a, b) in enumerate(ranges):
            duration = b - a
            chain = [f"atrim=start={a}:end={b}", "asetpts=PTS-STARTPTS"]
            if self.fade_time > 0 and duration > 2 * self.fade_time:
                # Skip fade-in on first range (start of file = already
                # silent) and fade-out on last range (end of file =
                # same). Every internal boundary gets both so neither
                # side of a cut has a raw edge.
                if i > 0:
                    chain.append(
                        f"afade=t=in:d={self.fade_time}:curve=log"
                    )
                if i < len(ranges) - 1:
                    chain.append(
                        f"afade=t=out:st={duration - self.fade_time}"
                        f":d={self.fade_time}:curve=log"
                    )
            lines.append(f"[0:a]{','.join(chain)}[a{i}]")
        concat_inputs = "".join(f"[a{i}]" for i in range(len(ranges)))
        lines.append(f"{concat_inputs}concat=n={len(ranges)}:v=0:a=1[aout]")
        return ";\n".join(lines), ["-map", "[aout]"]

    def encode(
        self,
        input_file: Path,
        output_file: Path,
        segments: list[Segment],
        media: Optional[MediaInfo] = None,
    ) -> subprocess.CompletedProcess:
        """Override parent's single-segment fast path too — raw
        `seg.start` / `seg.end` from the analyzer aren't keyframes,
        and `-c copy` needs keyframe boundaries."""
        media = media or MediaInfo()
        keyframes = self._probe_keyframes(input_file)
        if not keyframes:
            log.error("no keyframes found in input; cut encoder needs them")
            return subprocess.CompletedProcess(args=[], returncode=1)

        total = max((seg.end for seg in segments), default=0.0)
        ranges = self._snap_outward(segments, keyframes, total)
        kept = sum(b - a for a, b in ranges)
        requested = sum(seg.end - seg.start for seg in segments)
        console.print(
            f"snap: [yellow]{len(segments)}[/] segments → "
            f"[green]{len(ranges)}[/] copy range(s); "
            f"kept [green]{kept:.1f}s[/] "
            f"(requested {requested:.1f}s, "
            f"[dim]+{kept - requested:.1f}s from GOP alignment[/])"
        )
        for i, (a, b) in enumerate(ranges, 1):
            log.info(
                "%d. %s → %s (%.1fs)",
                i,
                format_timestamp(a),
                format_timestamp(b),
                b - a,
            )

        audio_args = self._audio_codec_args(media.audio)

        with tempfile.TemporaryDirectory(prefix="remsi_cut_") as tmpdir_str:
            tmpdir = Path(tmpdir_str)

            # ── 1) stream-copy each range to a video-only part
            console.rule("[bold yellow]Copy video[/]")
            entries: list[str] = []
            for i, (a, b) in enumerate(ranges):
                part_path = tmpdir / f"part{i:04d}{input_file.suffix}"
                result = self._copy_video_part(input_file, part_path, a, b)
                if result.returncode != 0:
                    return result
                entries.append(f"file '{part_path}'\n")

            # ── 2) concat the video parts (still pure stream-copy)
            console.rule("[bold yellow]Concat video[/]")
            concat_list = tmpdir / "concat.txt"
            concat_list.write_text("".join(entries))
            video_only = tmpdir / f"video{input_file.suffix}"
            cmd = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel", "warning",
                "-f", "concat",
                "-safe", "0",
                "-i", str(concat_list),
                "-c", "copy",
                "-an",
                "-y", str(video_only),
            ]
            result = self._run(cmd)
            if result.returncode != 0:
                return result

            # ── 3) build the faded audio in one filter_complex pass
            console.rule("[bold yellow]Fade audio[/]")
            script_content, audio_map = self._build_audio_filter(ranges)
            script_path = tmpdir / "audio.filter"
            script_path.write_text(script_content)
            log.debug("audio filter_complex:\n%s", script_content)
            audio_only = tmpdir / "audio.m4a"
            cmd = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel", "warning",
                "-i", str(input_file),
                "-/filter_complex", str(script_path),
                *audio_map,
                *audio_args,
                "-vn",
                "-y", str(audio_only),
            ]
            result = self._run(cmd)
            if result.returncode != 0:
                return result

            # ── 4) mux the two streams — both already final, just
            #      wrap them together in the output container.
            console.rule("[bold yellow]Mux[/]")
            cmd = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel", "warning",
                "-stats",
                "-i", str(video_only),
                "-i", str(audio_only),
                "-c", "copy",
                "-map", "0:v:0",
                "-map", "1:a:0",
            ]
            if self.force:
                cmd.append("-y")
            cmd.append(str(output_file))
            return self._run(cmd)

    def _encode_segments(
        self,
        input_file: Path,
        output_file: Path,
        segments: list[Segment],
        media: MediaInfo,
    ) -> subprocess.CompletedProcess:
        return self.encode(input_file, output_file, segments, media)


class SmartCutEncoder(Encoder):
    """Pure stream-copy cut. Snaps each kept segment OUTWARD to the
    surrounding keyframes, merges overlapping ranges, writes one
    `ffmpeg -ss -to -c copy` part per range, stitches with concat-
    demuxer. No re-encoding — the output re-uses the source's
    exact video packets.

    Tradeoffs:
      * No audio crossfades. Each stitch point is a hard packet
        boundary, may click slightly.
      * A silence shorter than one GOP can't be cut: its cut
        boundaries would land inside a GOP whose surrounding
        keyframes fall outside the silence, so the outward snap
        overlaps and the merger drops the cut. Shorter GOPs
        (lower `-g` on the source encode) = more cut precision.
      * Cuts may keep up to one GOP of silence at each boundary
        (the kept content is always fully preserved).

    For frame-accurate cuts with audio crossfades use FancyEncoder
    instead (`--mode fancy`)."""

    @staticmethod
    def _probe_keyframes(input_file: Path) -> list[float]:
        cmd = [
            "ffprobe",
            "-v", "error",
            "-select_streams", "v:0",
            "-skip_frame", "nokey",
            "-show_entries", "frame=pts_time",
            "-of", "csv=p=0",
            str(input_file),
        ]
        log.debug("spawn: %s", " ".join(cmd))
        result = subprocess.run(cmd, capture_output=True, text=True)
        keyframes: list[float] = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                keyframes.append(float(line))
            except ValueError:
                continue
        keyframes.sort()
        log.info("probed %d keyframes", len(keyframes))
        return keyframes

    @staticmethod
    def _snap_outward(
        segments: list[Segment], keyframes: list[float], total: float
    ) -> list[tuple[float, float]]:
        """Expand each segment's `[start, end]` outward to the nearest
        enclosing keyframes (`kf_before <= start`, `kf_after > end`),
        clamp to `[0, total]`, then merge overlaps.

        Two segments whose expansions collide (silence between them
        is shorter than one GOP) merge into one copy range — the
        silence stays in, because cutting it losslessly isn't
        possible."""
        if not keyframes:
            return [(seg.start, seg.end) for seg in segments]

        expanded: list[tuple[float, float]] = []
        for seg in segments:
            # Largest keyframe <= seg.start, or 0 if none precedes.
            idx_before = bisect.bisect_right(keyframes, seg.start) - 1
            kf_before = keyframes[idx_before] if idx_before >= 0 else 0.0
            # Smallest keyframe > seg.end, or total if none follows.
            idx_after = bisect.bisect_right(keyframes, seg.end)
            kf_after = keyframes[idx_after] if idx_after < len(keyframes) else total
            expanded.append((max(0.0, kf_before), min(total, kf_after)))

        expanded.sort(key=lambda r: r[0])
        merged: list[tuple[float, float]] = [expanded[0]]
        for a, b in expanded[1:]:
            pa, pb = merged[-1]
            if a <= pb:
                merged[-1] = (pa, max(pb, b))
            else:
                merged.append((a, b))
        return merged

    def _copy_range(
        self,
        input_file: Path,
        output_file: Path,
        start: float,
        end: float,
    ) -> subprocess.CompletedProcess:
        """Stream-copy `[start, end]` (absolute input seconds) out to
        `output_file`. `-ss` BEFORE `-i` is the fast container-level
        seek; since `start` is a real keyframe the seek is exact.
        `-to` with `-ss` before `-i` is an input-timeline absolute
        end marker, so the packet range is well-defined."""
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "warning",
            "-ss", str(start),
            "-to", str(end),
            "-i", str(input_file),
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            "-y", str(output_file),
        ]
        return self._run(cmd)

    def encode(
        self,
        input_file: Path,
        output_file: Path,
        segments: list[Segment],
        media: Optional[MediaInfo] = None,
    ) -> subprocess.CompletedProcess:
        """Override parent so the single-segment fast path also
        snaps. The parent assumes raw `seg.start` / `seg.end` are
        keyframes; for non-aligned cuts that produces garbage."""
        keyframes = self._probe_keyframes(input_file)
        if not keyframes:
            log.error("no keyframes found in input; smart-cut impossible")
            return subprocess.CompletedProcess(args=[], returncode=1)

        total = max((seg.end for seg in segments), default=0.0)
        ranges = self._snap_outward(segments, keyframes, total)
        kept = sum(b - a for a, b in ranges)
        requested = sum(seg.end - seg.start for seg in segments)
        console.print(
            f"snap: [yellow]{len(segments)}[/] segments → "
            f"[green]{len(ranges)}[/] copy range(s); "
            f"kept [green]{kept:.1f}s[/] "
            f"(requested {requested:.1f}s, "
            f"[dim]+{kept - requested:.1f}s from GOP alignment[/])"
        )
        for i, (a, b) in enumerate(ranges, 1):
            log.info(
                "%d. %s → %s (%.1fs)",
                i,
                format_timestamp(a),
                format_timestamp(b),
                b - a,
            )

        # Single range: one ffmpeg call straight into the output.
        if len(ranges) == 1:
            a, b = ranges[0]
            return self._copy_range(input_file, output_file, a, b)

        # Multi-range: write each to a tmp file, then concat-demux.
        with tempfile.TemporaryDirectory(prefix="remsi_copy_") as tmpdir_str:
            tmpdir = Path(tmpdir_str)
            entries: list[str] = []
            for i, (a, b) in enumerate(ranges):
                part_path = tmpdir / f"part{i:04d}{input_file.suffix}"
                result = self._copy_range(input_file, part_path, a, b)
                if result.returncode != 0:
                    return result
                entries.append(f"file '{part_path}'\n")

            console.rule("[bold yellow]Concat[/]")
            concat_list = tmpdir / "concat.txt"
            concat_list.write_text("".join(entries))

            cmd = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel", "warning",
                "-stats",
                "-f", "concat",
                "-safe", "0",
                "-i", str(concat_list),
                "-c", "copy",
            ]
            if self.force:
                cmd.append("-y")
            cmd.append(str(output_file))
            return self._run(cmd)

    def _encode_segments(
        self,
        input_file: Path,
        output_file: Path,
        segments: list[Segment],
        media: MediaInfo,
    ) -> subprocess.CompletedProcess:
        # `encode()` is overridden; this is unreachable but the abstract
        # contract requires an implementation. Delegate.
        return self.encode(input_file, output_file, segments, media)

# ── CLI orchestrator ─────────────────────────────────────────────────

class Remsi:
    """Removes silent + filler regions from video files via ffmpeg."""

    def __init__(
        self,
        analyzer: Analyzer,
        encoder: Encoder,
        min_cut: float,
        suffix: str,
        analyze_only: bool = False,
    ):
        self.analyzer = analyzer
        self.encoder = encoder
        self.min_cut = min_cut
        self.suffix = suffix
        self.analyze_only = analyze_only

    def process(self, input_file: Path, output_file: Path) -> None:
        console.rule(f"[bold yellow]{input_file.name}[/]")
        t_start = time.monotonic()

        total = self.analyzer.get_duration(input_file)
        if total is None:
            log.error("could not determine duration of %s", input_file)
            return
        media = Encoder.probe(input_file)
        console.print(f"duration: [bold]{format_timestamp(total)}[/]")
        console.print(f"size:     [yellow]{media.size_str}[/]")
        console.print(f"video:    [yellow]{media.video}[/]")
        console.print(f"audio:    [yellow]{media.audio}[/]")

        t_probe = time.monotonic()
        try:
            console.rule("[bold yellow]Silence detection[/]")
            silences = self.analyzer.detect_silence(input_file, total)
            console.print(f"found [yellow]{len(silences)}[/] silent region(s)")

            fillers: list[Region] = []
            if self.analyzer.stt_adapter:
                console.rule(
                    f"[bold yellow]Speech analysis ({self.analyzer.stt_adapter.name})[/]"
                )
                fillers = self.analyzer.detect_filler_words(input_file, silences)
                n_filler = sum(1 for r in fillers if r.kind == RegionKind.FILLER)
                n_stutter = sum(1 for r in fillers if r.kind == RegionKind.STUTTER)
                parts: list[str] = []
                if n_filler:
                    parts.append(f"[yellow]{n_filler}[/] filler(s)")
                if n_stutter:
                    parts.append(f"[yellow]{n_stutter}[/] stutter(s)")
                if parts:
                    console.print("found " + ", ".join(parts))
        except RuntimeError as e:
            log.error(str(e))
            return

        all_regions = Analyzer.merge_regions(silences, fillers)
        if self.min_cut > 0:
            skipped = [r for r in all_regions if (r.end - r.start) < self.min_cut]
            cut = [r for r in all_regions if (r.end - r.start) >= self.min_cut]
            if skipped:
                console.print(
                    f"skipping [yellow]{len(skipped)}[/] region(s) "
                    f"shorter than {self.min_cut}s"
                )
            all_regions = cut
        segments = self.analyzer.regions_to_segments(all_regions, total)

        if not all_regions:
            console.print("[dim]nothing to remove, skipping[/]")
            return

        kept = sum(seg.end - seg.start for seg in segments)
        removed = total - kept
        pct = (removed / total * 100) if total > 0 else 0
        console.print(
            f"removing [yellow]{removed:.1f}s[/] of {total:.1f}s "
            f"([bold]{pct:.1f}%[/])"
        )

        console.rule("[bold yellow]Timeline[/]")
        timeline = [
            Region(seg.start, seg.end, RegionKind.SPEECH) for seg in segments
        ] + list(all_regions)
        timeline.sort(key=lambda r: r.start)

        table = Table(show_header=True, header_style="bold", box=None)
        table.add_column("#", justify="right", style="dim")
        table.add_column("start")
        table.add_column("end")
        table.add_column("duration", justify="right")
        table.add_column("kind")
        for i, r in enumerate(timeline, 1):
            table.add_row(
                str(i),
                format_timestamp(r.start),
                format_timestamp(r.end),
                f"{r.end - r.start:.1f}s",
                f"[{r.kind.style}]{r.kind}[/]",
            )
        console.print(table)

        t_analysis = time.monotonic()
        if self.analyze_only:
            console.print(
                f"elapsed: [yellow]{t_analysis - t_start:.1f}s[/] "
                f"([dim]probe {t_probe - t_start:.1f}s · "
                f"analysis {t_analysis - t_probe:.1f}s[/])"
            )
            return

        console.rule("[bold yellow]Encoding[/]")
        if isinstance(self.encoder, SmartCutEncoder):
            console.print("mode:  [yellow]fast (smart-cut)[/]")
        console.print(f"output: [green]{output_file}[/]")
        try:
            result = self.encoder.encode(input_file, output_file, segments, media)
        except KeyboardInterrupt:
            log.error("interrupted, cleaning up…")
            sys.exit(130)
        if result.returncode != 0:
            log.error("ffmpeg exited with code %d", result.returncode)
            return

        console.rule(f"[bold green]{input_file.name} → {output_file.name}[/]")
        out_media = Encoder.probe(output_file)
        summary = Table(show_header=True, header_style="bold", box=None)
        summary.add_column("")
        summary.add_column("before", style="dim")
        summary.add_column("after", style="green")
        summary.add_row(
            "duration",
            format_timestamp(total),
            format_timestamp(kept),
        )
        summary.add_row("size", media.size_str, out_media.size_str)
        summary.add_row("video", str(media.video), str(out_media.video))
        summary.add_row("audio", str(media.audio), str(out_media.audio))
        console.print(summary)

        detected: list[str] = []
        if silences:
            detected.append(f"[yellow]{len(silences)}[/] silence(s)")
        n_filler = sum(1 for r in fillers if r.kind == RegionKind.FILLER)
        n_stutter = sum(1 for r in fillers if r.kind == RegionKind.STUTTER)
        if n_filler:
            detected.append(f"[yellow]{n_filler}[/] filler(s)")
        if n_stutter:
            detected.append(f"[yellow]{n_stutter}[/] stutter(s)")
        if detected:
            console.print("detected " + " and ".join(detected))
        console.print(
            f"trimmed [yellow]{removed:.1f}s[/] ([bold]{pct:.1f}%[/])"
        )
        t_end = time.monotonic()
        console.print(
            f"elapsed: [yellow]{t_end - t_start:.1f}s[/] "
            f"([dim]probe {t_probe - t_start:.1f}s · "
            f"analysis {t_analysis - t_probe:.1f}s · "
            f"encode {t_end - t_analysis:.1f}s[/])"
        )

    def run(self, inputs: list[Path], output: Optional[Path]) -> None:
        for input_file in inputs:
            if not input_file.exists():
                log.error("%s not found", input_file)
                continue
            if output is not None and len(inputs) == 1:
                output_file = output
            else:
                output_file = input_file.with_stem(f"{input_file.stem}-{self.suffix}")
            self.process(input_file, output_file)

    # ── CLI ──────────────────────────────────────────────────────────

    @click.command(
        "remsi",
        context_settings={"help_option_names": ["-h", "--help"]},
    )
    @click.argument(
        "inputs",
        nargs=-1,
        required=True,
        type=click.Path(path_type=Path, exists=False),
    )
    @click.option(
        "-o",
        "--output",
        type=click.Path(path_type=Path),
        help="Output path; single-input only.",
    )
    @click.option("-n", "--noise", default="-45dB", help="Silence threshold.")
    @click.option(
        "-d",
        "--duration",
        type=float,
        default=0.8,
        help="Minimum silence duration (s).",
    )
    @click.option(
        "--min-cut",
        type=float,
        default=0.1,
        help="Smallest region we'll cut (keeps shorter ones intact).",
    )
    @click.option(
        "--gpu",
        type=click.Choice(["nvidia", "amd", "vaapi", "auto", "none"]),
        default="auto",
        help="GPU encoder.",
    )
    @click.option("--codec", default=None, help="Force video codec.")
    @click.option(
        "--fade-time",
        type=float,
        default=0.1,
        help="Audio fade duration (s) at cut boundaries; 0 disables.",
    )
    @click.option(
        "--fade-video-filter",
        default="xfade:transition=fadefast",
        help="Video crossfade filter; fancy mode only.",
    )
    @click.option(
        "--fade-audio-filter",
        default="acrossfade:curve1=log:curve2=log",
        help="Audio crossfade filter; fancy mode only.",
    )
    @click.option("--suffix", default="silencer", help="Output filename suffix.")
    @click.option(
        "-w",
        "--with-whisper",
        is_flag=True,
        help="Enable filler-word detection via STT.",
    )
    @click.option(
        "--stt-provider",
        type=click.Choice([p.value for p in TranscriptionProvider]),
        default=TranscriptionProvider.WHISPER_CPP.value,
        help="STT provider.",
    )
    @click.option(
        "--whisper-cpp-model-dir",
        default="~/.local/share/applications/waystt/models",
        help="whisper.cpp GGML model directory.",
    )
    @click.option(
        "--whisper-cpp-model",
        default="ggml-large-v3.bin",
        help="whisper.cpp GGML model filename.",
    )
    @click.option(
        "--http-base-url",
        default="https://ai.kilic.dev/api/v1",
        help="HTTP STT base URL.",
    )
    @click.option(
        "--http-model",
        default="distil-large-v3",
        help="HTTP STT model name.",
    )
    @click.option(
        "--encoder",
        type=click.Choice([k.value for k in EncoderKind]),
        default=EncoderKind.CUT.value,
        help="Encoder pipeline.",
    )
    @click.option(
        "--analyze",
        is_flag=True,
        help="Analyze only; skip encoding.",
    )
    @click.option("-f", "--force", is_flag=True, help="Overwrite existing output.")
    @click.option("-v", "--verbose", is_flag=True, help="Enable debug logging.")
    def cli(
        inputs,
        output,
        noise,
        duration,
        min_cut,
        gpu,
        codec,
        fade_time,
        fade_video_filter,
        fade_audio_filter,
        suffix,
        with_whisper,
        stt_provider,
        whisper_cpp_model_dir,
        whisper_cpp_model,
        http_base_url,
        http_model,
        encoder,
        analyze,
        force,
        verbose,
    ):
        """Remove silent + filler regions from video files via ffmpeg."""
        create_logger(verbose)
        if output is not None and len(inputs) > 1:
            raise click.UsageError("-o/--output requires a single input file")

        # Transcription adapter — only built when STT is enabled.
        stt_adapter: Optional[TranscriptionAdapter] = None
        if with_whisper:
            match TranscriptionProvider(stt_provider):
                case TranscriptionProvider.WHISPER_CPP:
                    model_path = (
                        Path(whisper_cpp_model_dir).expanduser() / whisper_cpp_model
                    )
                    if not model_path.exists():
                        raise click.UsageError(f"model not found at {model_path}")
                    stt_adapter = TranscriptionAdapterWhisperCpp(model_path=model_path)
                case TranscriptionProvider.HTTP:
                    stt_adapter = TranscriptionAdapterHttp(
                        base_url=http_base_url,
                        model=http_model,
                        api_key=os.environ.get("AI_KILIC_DEV_API_KEY", ""),
                    )
                case _:
                    raise click.UsageError(f"unknown stt provider: {stt_provider!r}")

        # GPU resolution — explicit name, auto-detect, or disable.
        resolved_gpu: Optional[str]
        match gpu:
            case "auto":
                resolved_gpu = Encoder.detect_gpu()
                if resolved_gpu:
                    log.info("GPU encoder: [green]%s[/]", resolved_gpu)
                else:
                    log.warning("no GPU encoder found; using software encoding")
            case "none":
                resolved_gpu = None
            case _:
                resolved_gpu = gpu

        pipeline: Encoder
        match EncoderKind(encoder):
            case EncoderKind.CUT:
                pipeline = CutEncoder(
                    gpu=resolved_gpu,
                    codec=codec,
                    fade_time=fade_time,
                    force=force,
                )
            case EncoderKind.SMART_CUT:
                pipeline = SmartCutEncoder(
                    gpu=resolved_gpu,
                    codec=codec,
                    force=force,
                )
            case EncoderKind.FANCY:
                pipeline = FancyEncoder(
                    gpu=resolved_gpu,
                    codec=codec,
                    fade_time=fade_time,
                    video_filter=fade_video_filter,
                    audio_filter=fade_audio_filter,
                    force=force,
                )
            case _:
                raise click.UsageError(f"unknown encoder: {encoder!r}")

        analyzer = Analyzer(noise=noise, duration=duration, stt_adapter=stt_adapter)

        Remsi(
            analyzer=analyzer,
            encoder=pipeline,
            min_cut=min_cut,
            suffix=suffix,
            analyze_only=analyze,
        ).run(list(inputs), output)

if __name__ == "__main__":
    try:
        Remsi.cli()
    except KeyboardInterrupt:
        log.error("interrupted")
        sys.exit(130)
