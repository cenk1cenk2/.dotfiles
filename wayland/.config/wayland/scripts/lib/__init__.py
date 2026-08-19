"""Shared building blocks for the Wayland scripts in this folder.

Re-exports follow the `X as X` form so Ruff treats them as explicit
public re-exports (silences F401) and LSP rename still works — those
are real imported names, not string literals in `__all__`."""

from .cli import (
    RunResult as RunResult,
    create_logger as create_logger,
    run as run,
)
from .desktop import (
    is_headless as is_headless,
    set_headless as set_headless,
)
from .enrich import (
    DEFAULT_API_KEY_ENV as DEFAULT_API_KEY_ENV,
    DEFAULT_BASE_URL as DEFAULT_BASE_URL,
    DEFAULT_ENRICH_ADAPTER as DEFAULT_ENRICH_ADAPTER,
    DEFAULT_TIMEOUT as DEFAULT_TIMEOUT,
    THINKING_LEVELS as THINKING_LEVELS,
    EnrichAdapter as EnrichAdapter,
    EnrichAdapterHttp as EnrichAdapterHttp,
    EnrichAdapterHyprpilot as EnrichAdapterHyprpilot,
    EnrichProvider as EnrichProvider,
    EnrichSpec as EnrichSpec,
    build_enricher as build_enricher,
    enrich_options as enrich_options,
    spec_from_options as spec_from_options,
)
from .input import (
    InputAdapter as InputAdapter,
    InputAdapterClipboard as InputAdapterClipboard,
    InputAdapterFile as InputAdapterFile,
    InputAdapterStdin as InputAdapterStdin,
    InputMode as InputMode,
    build_input as build_input,
)
from .notify import notify as notify
from .output import (
    OutputAdapter as OutputAdapter,
    OutputAdapterClipboard as OutputAdapterClipboard,
    OutputAdapterFile as OutputAdapterFile,
    OutputAdapterStdout as OutputAdapterStdout,
    OutputAdapterType as OutputAdapterType,
    OutputMode as OutputMode,
    build_output as build_output,
)
from .prompts import load_prompt as load_prompt
from .prompts import load_relative_file as load_relative_file
from .stt import (
    DEFAULT_STT_LANGUAGE as DEFAULT_STT_LANGUAGE,
    DEFAULT_STT_MODEL as DEFAULT_STT_MODEL,
    DEFAULT_STT_TIMEOUT as DEFAULT_STT_TIMEOUT,
    PLAIN_FORMATS as PLAIN_FORMATS,
    ResponseFormat as ResponseFormat,
    SttAdapter as SttAdapter,
    SttAdapterHttp as SttAdapterHttp,
    SttAdapterHyprwhspr as SttAdapterHyprwhspr,
    SttProvider as SttProvider,
    SttRecorder as SttRecorder,
    SttSpec as SttSpec,
)
from .tts import (
    DEFAULT_TTS_MAX_CHARS as DEFAULT_TTS_MAX_CHARS,
    DEFAULT_TTS_PLAYER as DEFAULT_TTS_PLAYER,
    DEFAULT_TTS_SAMPLE_RATE as DEFAULT_TTS_SAMPLE_RATE,
    DEFAULT_TTS_TIMEOUT as DEFAULT_TTS_TIMEOUT,
    DEFAULT_TTS_VOICE as DEFAULT_TTS_VOICE,
    AudioFormat as AudioFormat,
    PlayerAdapter as PlayerAdapter,
    PlayerAdapterFfplay as PlayerAdapterFfplay,
    PlayerAdapterPaplay as PlayerAdapterPaplay,
    PlayerAdapterPwCat as PlayerAdapterPwCat,
    PlayerMode as PlayerMode,
    TeeReader as TeeReader,
    TtsAdapterHttp as TtsAdapterHttp,
    TtsSpec as TtsSpec,
    copy_audio as copy_audio,
)
from .waybar import signal_waybar as signal_waybar
