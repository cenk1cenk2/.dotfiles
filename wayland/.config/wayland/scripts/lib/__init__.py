"""Shared building blocks for the Wayland scripts in this folder.

Re-exports follow the `X as X` form so Ruff treats them as explicit
public re-exports (silences F401) and LSP rename still works — those
are real imported names, not string literals in `__all__`."""

from .enrich import (
    DEFAULT_API_KEY_ENV as DEFAULT_API_KEY_ENV,
)
from .enrich import (
    DEFAULT_BASE_URL as DEFAULT_BASE_URL,
)
from .enrich import (
    DEFAULT_ENRICH_ADAPTER as DEFAULT_ENRICH_ADAPTER,
)
from .enrich import (
    DEFAULT_TIMEOUT as DEFAULT_TIMEOUT,
)
from .enrich import (
    THINKING_LEVELS as THINKING_LEVELS,
)
from .enrich import (
    EnrichAdapter as EnrichAdapter,
)
from .enrich import (
    EnrichAdapterHttp as EnrichAdapterHttp,
)
from .enrich import (
    EnrichAdapterHyprpilot as EnrichAdapterHyprpilot,
)
from .enrich import (
    EnrichProvider as EnrichProvider,
)
from .enrich import (
    EnrichSpec as EnrichSpec,
)
from .enrich import (
    build_enricher as build_enricher,
)
from .enrich import (
    enrich_options as enrich_options,
)
from .enrich import (
    spec_from_options as spec_from_options,
)
from .input import (
    InputAdapter as InputAdapter,
)
from .input import (
    InputAdapterClipboard as InputAdapterClipboard,
)
from .input import (
    InputAdapterFile as InputAdapterFile,
)
from .input import (
    InputAdapterStdin as InputAdapterStdin,
)
from .input import (
    InputMode as InputMode,
)
from .input import (
    build_input as build_input,
)
from .output import (
    OutputAdapter as OutputAdapter,
)
from .output import (
    OutputAdapterClipboard as OutputAdapterClipboard,
)
from .output import (
    OutputAdapterFile as OutputAdapterFile,
)
from .output import (
    OutputAdapterStdout as OutputAdapterStdout,
)
from .output import (
    OutputAdapterType as OutputAdapterType,
)
from .output import (
    OutputMode as OutputMode,
)
from .output import (
    build_output as build_output,
)
from .prompts import load_prompt as load_prompt
from .prompts import load_relative_file as load_relative_file
from .stt import (
    DEFAULT_STT_LANGUAGE as DEFAULT_STT_LANGUAGE,
)
from .stt import (
    DEFAULT_STT_MODEL as DEFAULT_STT_MODEL,
)
from .stt import (
    DEFAULT_STT_TIMEOUT as DEFAULT_STT_TIMEOUT,
)
from .stt import (
    PLAIN_FORMATS as PLAIN_FORMATS,
)
from .stt import (
    ResponseFormat as ResponseFormat,
)
from .stt import (
    SttAdapter as SttAdapter,
)
from .stt import (
    SttAdapterHttp as SttAdapterHttp,
)
from .stt import (
    SttAdapterHyprwhspr as SttAdapterHyprwhspr,
)
from .stt import (
    SttProvider as SttProvider,
)
from .stt import (
    SttRecorder as SttRecorder,
)
from .stt import (
    SttSpec as SttSpec,
)
from .tts import (
    DEFAULT_TTS_LOUDNESS as DEFAULT_TTS_LOUDNESS,
)
from .tts import (
    DEFAULT_TTS_PLAYER as DEFAULT_TTS_PLAYER,
)
from .tts import (
    DEFAULT_TTS_SAMPLE_RATE as DEFAULT_TTS_SAMPLE_RATE,
)
from .tts import (
    DEFAULT_TTS_TIMEOUT as DEFAULT_TTS_TIMEOUT,
)
from .tts import (
    DEFAULT_TTS_VOICE as DEFAULT_TTS_VOICE,
)
from .tts import (
    AudioFormat as AudioFormat,
)
from .tts import (
    PlayerAdapter as PlayerAdapter,
)
from .tts import (
    PlayerAdapterFfplay as PlayerAdapterFfplay,
)
from .tts import (
    PlayerAdapterPaplay as PlayerAdapterPaplay,
)
from .tts import (
    PlayerAdapterPwCat as PlayerAdapterPwCat,
)
from .tts import (
    PlayerMode as PlayerMode,
)
from .tts import (
    TeeReader as TeeReader,
)
from .tts import (
    TtsAdapterHttp as TtsAdapterHttp,
)
from .tts import (
    TtsSpec as TtsSpec,
)
from .tts import (
    copy_audio as copy_audio,
)
from .waybar import signal_waybar as signal_waybar
