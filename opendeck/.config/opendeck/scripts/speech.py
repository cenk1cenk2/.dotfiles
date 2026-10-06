#!/usr/bin/env -S sh -c 'exec uv run --project "$(dirname "$0")" "$0" "$@"'

from __future__ import annotations

import json
import logging
import os
import socket
import time
from enum import IntEnum, StrEnum

from deck import Key, Plugin, command


class Session(StrEnum):
    STT = "stt"
    TTS = "tts"


class Action(StrEnum):
    TOGGLE = "dev.kilic.speech.toggle"
    PAUSE = "dev.kilic.speech.pause"
    KILL = "dev.kilic.speech.kill"


class ToggleLook(IntEnum):
    IDLE = 0
    LIVE = 1
    WORKING = 2
    PAUSED = 3


class PauseLook(IntEnum):
    IDLE = 0
    ACTIVE = 1
    PAUSED = 2


class KillLook(IntEnum):
    IDLE = 0
    ACTIVE = 1


class SpeechKey(Key):
    @property
    def session(self) -> Session:
        return Session(self.settings["session"])

    @property
    def args(self) -> list[str]:
        return list(self.settings.get("args", []))

    def flag(self, name: str) -> str | None:
        args = self.args
        if name not in args or args.index(name) + 1 >= len(args):
            return None

        return args[args.index(name) + 1]


class SpeechPlugin(Plugin):
    """Stream Deck plugin for speech.py: live STT/TTS keys.

    State comes from the sessions' control sockets, the same `status` request
    waybar's module makes; presses run speech.py itself, so its flags stay
    the only place behaviour is defined."""

    # Through zsh so the run picks up the API keys `.zshenv` exports, which
    # OpenDeck's own environment does not carry.
    SPEECH = ("zsh", "-c", '~/.config/wayland/scripts/speech.py "$@"', "zsh")
    # A press only spawns speech.py, whose socket appears a second or so later
    # once uv and its imports are up; until then the claim waits.
    CLAIM_SECONDS = 10.0
    KEY = SpeechKey

    log = logging.getLogger("speech-deck")

    def __init__(self, ws):
        super().__init__(ws)
        self.states: dict[Session, dict | None] = {}
        # The TTS session reports no style, so the key that started it is
        # remembered to tell `read` from `summary`.
        self.tts_owner: str | None = None
        self.tts_claimed_at = 0.0
        self.tts_seen = False

    @staticmethod
    def status(session: Session) -> dict | None:
        """Ask a session's control socket for its state; None when idle."""
        runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(1)
        try:
            sock.connect(os.path.join(runtime, f"wayland-{session}.sock"))
            sock.sendall(json.dumps({"cmd": "status"}).encode() + b"\n")
            chunks = []
            while data := sock.recv(4096):
                chunks.append(data)
        except OSError:
            return None
        finally:
            sock.close()

        try:
            reply = json.loads(b"".join(chunks))
        except ValueError:
            return None

        return reply.get("state") if reply.get("ok") else None

    def speech(self, session: Session, verb: str, *args: str) -> None:
        self.spawn([*self.SPEECH, session.value, verb, *args])

    def look_toggle(self, context: str, key: SpeechKey) -> tuple[ToggleLook, str]:
        state = self.states.get(key.session)
        if state is None:
            return ToggleLook.IDLE, ""

        match key.session:
            case Session.STT:
                if state["output"] != key.flag("--output") or bool(
                    state.get("enrich")
                ) != ("--enrich" in key.args):
                    return ToggleLook.IDLE, ""
                if state.get("paused"):
                    return ToggleLook.PAUSED, "paused"

                return {
                    "recording": (ToggleLook.LIVE, "rec"),
                    "working": (ToggleLook.WORKING, "working"),
                    "output": (ToggleLook.WORKING, "output"),
                }[state["phase"]]

            case Session.TTS:
                if context != self.tts_owner:
                    return ToggleLook.IDLE, ""
                if state.get("paused"):
                    return ToggleLook.PAUSED, "paused"
                if state["phase"] == "working":
                    return ToggleLook.WORKING, "working"

                chars = int(state["chars"])
                text = f"{chars / 1000:.1f}k" if chars >= 1000 else str(chars)
                if (tempo := float(state.get("tempo") or 1.0)) != 1.0:
                    text += f" {tempo:g}x"
                if queued := state.get("queued"):
                    text += f" +{len(queued)}"

                return ToggleLook.LIVE, text

    def look(self, context: str, key: SpeechKey) -> tuple[int, str]:
        state = self.states.get(key.session)
        match key.action:
            case Action.TOGGLE:
                return self.look_toggle(context, key)
            case Action.PAUSE if state is None:
                return PauseLook.IDLE, ""
            case Action.PAUSE if state.get("paused"):
                return PauseLook.PAUSED, "paused"
            case Action.PAUSE:
                return PauseLook.ACTIVE, ""
            case Action.KILL if state is None:
                return KillLook.IDLE, ""
            case Action.KILL:
                return KillLook.ACTIVE, ""

        raise ValueError(f"unknown action {key.action}")

    def poll(self) -> None:
        sessions = {key.session for key in self.keys.values()}
        self.states = {session: self.status(session) for session in sessions}
        if self.states.get(Session.TTS) is not None:
            self.tts_seen = True
        elif self.tts_owner is not None and (
            self.tts_seen or time.monotonic() - self.tts_claimed_at > self.CLAIM_SECONDS
        ):
            self.tts_owner, self.tts_seen = None, False

    def press(self, context: str, key: SpeechKey) -> None:
        match key.action:
            case Action.TOGGLE:
                if key.session is Session.TTS and self.states.get(Session.TTS) is None:
                    self.tts_owner, self.tts_seen = context, False
                    self.tts_claimed_at = time.monotonic()
                self.speech(key.session, "toggle", *key.args)
            case Action.PAUSE:
                self.speech(key.session, "pause")
            case Action.KILL:
                self.speech(key.session, "kill")

    def holds(self, key: SpeechKey) -> bool:
        return key.action == Action.TOGGLE

    def hold(self, context: str, key: SpeechKey) -> None:
        self.speech(key.session, "kill")


if __name__ == "__main__":
    command(SpeechPlugin)()
