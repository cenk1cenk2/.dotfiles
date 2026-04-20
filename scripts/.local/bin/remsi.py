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

# ── Logging ──────────────────────────────────────────────────────────

_console: Optional[Console] = None

def create_logger(verbose: bool) -> logging.Logger:
    """Install a rich handler on the root logger bound to stderr."""
    global _console
    root = logging.getLogger()
    level = logging.DEBUG if verbose else logging.INFO
    root.setLevel(level)
    if not any(isinstance(h, RichHandler) for h in root.handlers):
        if _console is None:
            _console = Console(file=sys.stderr, stderr=True, force_terminal=None)
        for h in list(root.handlers):
            root.removeHandler(h)
        handler = RichHandler(
            console=_console,
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
                    "  filler: %r %s → %s",
                    text,
                    format_timestamp(w.start),
                    format_timestamp(w.end),
                )
                prev_letters = None
            elif letters and letters == prev_letters:
                stutters.append(Region(w.start, w.end, RegionKind.STUTTER))
                log.debug(
                    "  stutter: %r %s → %s",
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
                "  %d. %s → %s (%.1fs) [%s]%s[/]",
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

class EncoderMode(StrEnum):
    FAST = "fast"
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

class SmartCutEncoder(Encoder):
    """Keyframe-aware smart-cut: stream-copy the cuttable parts,
    re-encode only segments that don't align to keyframes. Much
    faster than FancyEncoder for long videos at the cost of a one-
    sample afade at the stitch points."""

    def __init__(
        self,
        gpu: Optional[str],
        codec: Optional[str],
        fade_time: float,
        fade_curve: str,
        force: bool = False,
    ):
        super().__init__(gpu, codec, force)
        self.fade_time = fade_time
        self.fade_curve = fade_curve

    @staticmethod
    def _probe_keyframes(input_file: Path) -> list[float]:
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-skip_frame",
            "nokey",
            "-show_entries",
            "frame=pts_time",
            "-of",
            "csv=p=0",
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
    def _split_at_keyframes(
        seg_start: float, seg_end: float, keyframes: list[float]
    ) -> list[tuple[float, float, bool]]:
        if not keyframes:
            return [(seg_start, seg_end, True)]

        idx_start = bisect.bisect_left(keyframes, seg_start)
        kf_start = keyframes[idx_start] if idx_start < len(keyframes) else None
        idx_end = bisect.bisect_right(keyframes, seg_end) - 1
        kf_end = keyframes[idx_end] if idx_end >= 0 else None

        if kf_start is None or kf_end is None or kf_start >= seg_end:
            return [(seg_start, seg_end, True)]

        parts: list[tuple[float, float, bool]] = []
        if kf_start > seg_start:
            parts.append((seg_start, kf_start, True))
        if kf_end > kf_start:
            parts.append((kf_start, kf_end, False))
        elif kf_start < seg_end:
            parts.append((kf_start, seg_end, True))
            return parts
        if seg_end > kf_end:
            parts.append((kf_end, seg_end, True))
        return parts if parts else [(seg_start, seg_end, True)]

    @staticmethod
    def _encode_part(
        input_file: Path,
        part_path: Path,
        start: float,
        end: float,
        video_args: list[str],
        audio_args: list[str],
        fade_time: float = 0,
        fade_curve: str = "log",
        fade_in: bool = False,
        fade_out: bool = False,
    ) -> subprocess.CompletedProcess:
        duration = end - start
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-ss",
            str(start),
            "-i",
            str(input_file),
            "-t",
            str(duration),
        ]
        cmd.extend(video_args)
        af_parts: list[str] = []
        if fade_in and fade_time > 0 and duration > fade_time:
            af_parts.append(f"afade=t=in:d={fade_time}:curve={fade_curve}")
        if fade_out and fade_time > 0 and duration > fade_time:
            af_parts.append(
                f"afade=t=out:st={duration - fade_time}:d={fade_time}:curve={fade_curve}"
            )
        if af_parts:
            cmd.extend(["-af", ",".join(af_parts)])
        cmd.extend(audio_args)
        cmd.extend(["-y", str(part_path)])
        log.info("spawn: %s", " ".join(cmd))
        return subprocess.run(cmd)

    def _encode_segments(  # noqa: C901
        self,
        input_file: Path,
        output_file: Path,
        segments: list[Segment],
        media: MediaInfo,
    ) -> subprocess.CompletedProcess:
        keyframes = self._probe_keyframes(input_file)
        video_args = self._video_codec_args(media.video)
        audio_args = self._audio_codec_args(media.audio)

        all_parts: list[tuple[float, float, bool]] = []
        for seg in segments:
            sub = self._split_at_keyframes(seg.start, seg.end, keyframes)
            for start, end, reencode in sub:
                log.info(
                    "  part %s → %s (%s)",
                    format_timestamp(start),
                    format_timestamp(end),
                    "reencode" if reencode else "copy",
                )
            all_parts.extend(sub)

        copy_dur = sum(e - s for s, e, r in all_parts if not r)
        reencode_dur = sum(e - s for s, e, r in all_parts if r)
        log.info(
            "smart-cut: [green]%.1fs[/] stream copy · [yellow]%.1fs[/] re-encode",
            copy_dur,
            reencode_dur,
        )

        with tempfile.TemporaryDirectory(prefix="remsi_fast_") as tmpdir_str:
            tmpdir = Path(tmpdir_str)
            concat_entries: list[str] = []
            reencode_idx = 0
            n = len(all_parts)
            for i, (start, end, reencode) in enumerate(all_parts, 1):
                mode = "reencode" if reencode else "copy"
                log.info(
                    "  [%d/%d] %s → %s (%s)",
                    i,
                    n,
                    format_timestamp(start),
                    format_timestamp(end),
                    mode,
                )
                if reencode:
                    part_path = tmpdir / f"part{reencode_idx:04d}{input_file.suffix}"
                    reencode_idx += 1
                    idx = i - 1
                    fade_in = idx > 0 and not all_parts[idx - 1][2]
                    fade_out = idx < n - 1 and not all_parts[idx + 1][2]
                    result = self._encode_part(
                        input_file,
                        part_path,
                        start,
                        end,
                        video_args,
                        audio_args,
                        fade_time=self.fade_time,
                        fade_curve=self.fade_curve,
                        fade_in=fade_in,
                        fade_out=fade_out,
                    )
                    if result.returncode != 0:
                        return result
                    concat_entries.append(f"file '{part_path}'\n")
                else:
                    part_path = tmpdir / f"part{reencode_idx:04d}{input_file.suffix}"
                    reencode_idx += 1
                    cmd = [
                        "ffmpeg",
                        "-hide_banner",
                        "-loglevel",
                        "warning",
                        "-i",
                        str(input_file),
                        "-ss",
                        str(start),
                        "-t",
                        str(end - start),
                        "-c",
                        "copy",
                        "-avoid_negative_ts",
                        "make_zero",
                        "-y",
                        str(part_path),
                    ]
                    result = self._run(cmd)
                    if result.returncode != 0:
                        return result
                    concat_entries.append(f"file '{part_path}'\n")

            log.info("[bold yellow]── concat ──[/]")
            concat_list = tmpdir / "concat.txt"
            with open(concat_list, "w") as f:
                f.writelines(concat_entries)

            cmd = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "warning",
                "-stats",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_list),
                "-c:v",
                "copy",
            ]
            cmd.extend(audio_args)
            if self.force:
                cmd.append("-y")
            cmd.append(str(output_file))
            return self._run(cmd)

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
        log.info("[bold yellow]── %s ──[/]", input_file.name)
        t_start = time.monotonic()

        total = self.analyzer.get_duration(input_file)
        if total is None:
            log.error("could not determine duration of %s", input_file)
            return
        media = Encoder.probe(input_file)
        log.info("duration: [bold]%s[/]", format_timestamp(total))
        log.info("size: [yellow]%s[/]", media.size_str)
        log.info("video: [yellow]%s[/]", media.video)
        log.info("audio: [yellow]%s[/]", media.audio)

        t_probe = time.monotonic()
        try:
            log.info("[bold yellow]── silence detection ──[/]")
            silences = self.analyzer.detect_silence(input_file, total)
            log.info("found [yellow]%d[/] silent region(s)", len(silences))

            fillers: list[Region] = []
            if self.analyzer.stt_adapter:
                log.info(
                    "[bold yellow]── speech analysis (%s) ──[/]",
                    self.analyzer.stt_adapter.name,
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
                    log.info("found %s", ", ".join(parts))
        except RuntimeError as e:
            log.error(str(e))
            return

        all_regions = Analyzer.merge_regions(silences, fillers)
        if self.min_cut > 0:
            skipped = [r for r in all_regions if (r.end - r.start) < self.min_cut]
            cut = [r for r in all_regions if (r.end - r.start) >= self.min_cut]
            if skipped:
                log.info(
                    "skipping [yellow]%d[/] region(s) shorter than %ss",
                    len(skipped),
                    self.min_cut,
                )
            all_regions = cut
        segments = self.analyzer.regions_to_segments(all_regions, total)

        if not all_regions:
            log.info("[dim]nothing to remove, skipping[/]")
            return

        kept = sum(seg.end - seg.start for seg in segments)
        removed = total - kept
        pct = (removed / total * 100) if total > 0 else 0
        log.info(
            "removing [yellow]%.1fs[/] of %.1fs ([bold]%.1f%%[/])",
            removed,
            total,
            pct,
        )

        log.info("[bold yellow]── summary ──[/]")
        timeline = [
            Region(seg.start, seg.end, RegionKind.SPEECH) for seg in segments
        ] + list(all_regions)
        timeline.sort(key=lambda r: r.start)
        for i, r in enumerate(timeline, 1):
            log.info(
                "[dim]%3d.[/] %s → %s [dim](%.1fs)[/] [%s]%s[/]",
                i,
                format_timestamp(r.start),
                format_timestamp(r.end),
                r.end - r.start,
                r.kind.style,
                r.kind,
            )

        t_analysis = time.monotonic()
        if self.analyze_only:
            log.info(
                "elapsed: [yellow]%.1fs[/] (probe %.1fs · analysis %.1fs)",
                t_analysis - t_start,
                t_probe - t_start,
                t_analysis - t_probe,
            )
            return

        log.info("[bold yellow]── encoding ──[/]")
        if isinstance(self.encoder, SmartCutEncoder):
            log.info("mode: [yellow]fast (smart-cut)[/]")
        log.info("writing [green]%s[/]", output_file)
        try:
            result = self.encoder.encode(input_file, output_file, segments, media)
        except KeyboardInterrupt:
            log.error("interrupted, cleaning up…")
            sys.exit(130)
        if result.returncode != 0:
            log.error("ffmpeg exited with code %d", result.returncode)
            return

        log.info("[bold yellow]── %s → %s ──[/]", input_file.name, output_file.stem)
        parts = []
        if silences:
            parts.append(f"[yellow]{len(silences)}[/] silence(s)")
        n_filler = sum(1 for r in fillers if r.kind == RegionKind.FILLER)
        n_stutter = sum(1 for r in fillers if r.kind == RegionKind.STUTTER)
        if n_filler:
            parts.append(f"[yellow]{n_filler}[/] filler(s)")
        if n_stutter:
            parts.append(f"[yellow]{n_stutter}[/] stutter(s)")
        if parts:
            log.info("detected %s", " and ".join(parts))
        log.info(
            "trimmed [yellow]%.1fs[/] ([bold]%.1f%%[/]), %s down to [green]%s[/]",
            removed,
            pct,
            format_timestamp(total),
            format_timestamp(kept),
        )
        out_media = Encoder.probe(output_file)
        log.info("size: [yellow]%s[/]", out_media.size_str)
        log.info("video: [yellow]%s[/]", out_media.video)
        log.info("audio: [yellow]%s[/]", out_media.audio)
        t_end = time.monotonic()
        log.info(
            "elapsed: [yellow]%.1fs[/] (probe %.1fs · analysis %.1fs · encode %.1fs)",
            t_end - t_start,
            t_probe - t_start,
            t_analysis - t_probe,
            t_end - t_analysis,
        )
        log.info("[green]%s[/]", output_file)

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
        help="Crossfade duration (s); 0 disables.",
    )
    @click.option(
        "--fade-video-filter",
        default="xfade:transition=fadefast",
        help="Video crossfade filter spec.",
    )
    @click.option(
        "--fade-audio-filter",
        default="acrossfade:curve1=log:curve2=log",
        help="Audio crossfade filter spec.",
    )
    @click.option(
        "--fade-curve",
        default="log",
        help="afade curve for smart-cut stitch points.",
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
        "--mode",
        type=click.Choice([m.value for m in EncoderMode]),
        default=EncoderMode.FANCY.value,
        help="Encoder mode.",
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
        fade_curve,
        suffix,
        with_whisper,
        stt_provider,
        whisper_cpp_model_dir,
        whisper_cpp_model,
        http_base_url,
        http_model,
        mode,
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

        encoder: Encoder
        match EncoderMode(mode):
            case EncoderMode.FAST:
                encoder = SmartCutEncoder(
                    gpu=resolved_gpu,
                    codec=codec,
                    fade_time=fade_time,
                    fade_curve=fade_curve,
                    force=force,
                )
            case EncoderMode.FANCY:
                encoder = FancyEncoder(
                    gpu=resolved_gpu,
                    codec=codec,
                    fade_time=fade_time,
                    video_filter=fade_video_filter,
                    audio_filter=fade_audio_filter,
                    force=force,
                )
            case _:
                raise click.UsageError(f"unknown encoder mode: {mode!r}")

        analyzer = Analyzer(noise=noise, duration=duration, stt_adapter=stt_adapter)

        Remsi(
            analyzer=analyzer,
            encoder=encoder,
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
