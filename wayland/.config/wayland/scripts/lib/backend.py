"""Which speech server the calls are meant for.

Both answer on the same URL; the gateway tells them apart by a header, so
the choice is a header plus the dialect each one speaks, and the default
below is the only line a cutover has to touch."""

from __future__ import annotations

from enum import StrEnum


class SpeechBackend(StrEnum):
    SPEACHES = "speaches"
    LOCALAI = "localai"

    @property
    def headers(self) -> tuple[tuple[str, str], ...]:
        """What the gateway routes on; speaches is what it picks without one."""
        if self is SpeechBackend.LOCALAI:
            return (("x-speech-backend", self.value),)

        return ()


DEFAULT_SPEECH_BACKEND = SpeechBackend.SPEACHES
SPEECH_BACKEND_ENV = "SPEECH_BACKEND"
