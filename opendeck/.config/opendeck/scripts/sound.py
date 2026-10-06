#!/usr/bin/env -S sh -c 'exec uv run --project "$(dirname "$0")" "$0" "$@"'

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import ClassVar

import pulsectl
from dotlib.cli import run

from deck import Key, Plugin, command


class Action(StrEnum):
    VOLUME = "dev.kilic.sound.volume"
    MEDIA = "dev.kilic.sound.media"
    TALK = "dev.kilic.sound.talk"


class Device(StrEnum):
    OUTPUT = "output"
    INPUT = "input"


class VolumeLook(IntEnum):
    IDLE = 0
    MUTED = 1


class MediaLook(IntEnum):
    IDLE = 0
    PLAYING = 1


class TalkLook(IntEnum):
    MUTED = 0
    LIVE = 1


@dataclass
class Level:
    volume: int
    muted: bool


class SoundKey(Key):
    @property
    def device(self) -> Device:
        return Device(self.settings["device"])

    @property
    def verb(self) -> str:
        return self.settings["verb"]


class SoundPlugin(Plugin):
    """Stream Deck plugin for the sound keys: live levels, mute and player.

    Levels come from PulseAudio over one persistent connection; presses run
    the same `launch-app.py` dispatchers as the Hyprland binds, so the
    swayosd popup still shows."""

    LAUNCH = Path.home() / ".config/hypr/scripts/launch-app.py"
    # Dispatcher names per device and verb, as `definitions.lua` spells them.
    VOLUME: ClassVar[dict[Device, dict[str, str]]] = {
        Device.OUTPUT: {
            "down": "volume.down",
            "mute": "volume.mute",
            "up": "volume.up",
        },
        Device.INPUT: {
            "down": "volume.mic_down",
            "mute": "volume.mic_mute",
            "up": "volume.mic_up",
        },
    }
    MEDIA: ClassVar[dict[str, str]] = {
        "toggle": "media.toggle",
        "stop": "media.stop",
        "prev": "media.prev",
        "next": "media.next",
        "shift": "media.shift",
    }
    # The player is asked through two processes a round, so it is polled less
    # often than the levels.
    PLAYER_SECONDS = 1.0
    KEY = SoundKey

    log = logging.getLogger("sound-deck")

    def __init__(self, ws):
        super().__init__(ws)
        self.pulse: pulsectl.Pulse | None = None
        self.levels: dict[Device, Level] = {}
        self.player: str | None = None
        self.playing = False
        self.next_player = 0.0
        # The input's mute state before a push-to-talk press, restored on
        # release so the press never fights the mute key.
        self.talk_restore: bool | None = None

    def connect(self) -> pulsectl.Pulse:
        if self.pulse is None:
            self.pulse = pulsectl.Pulse("opendeck-sound")

        return self.pulse

    def default(self, device: Device):
        pulse = self.connect()
        info = pulse.server_info()
        match device:
            case Device.OUTPUT:
                return pulse.get_sink_by_name(info.default_sink_name)
            case Device.INPUT:
                return pulse.get_source_by_name(info.default_source_name)

    def poll_levels(self) -> None:
        try:
            for device in Device:
                target = self.default(device)
                self.levels[device] = Level(
                    round(target.volume.value_flat * 100), bool(target.mute)
                )
        except pulsectl.PulseError, pulsectl.PulseDisconnected:
            self.log.debug("pulse unavailable, reconnecting next poll")
            self.pulse = None
            self.levels = {}

    def poll_player(self) -> None:
        try:
            names = run(
                [
                    "busctl",
                    "--user",
                    "get-property",
                    "org.mpris.MediaPlayer2.playerctld",
                    "/org/mpris/MediaPlayer2",
                    "com.github.altdesktop.playerctld",
                    "PlayerNames",
                ],
                log=self.log,
                timeout=1,
            )
            status = run(
                ["playerctl", "-p", "playerctld", "status"], log=self.log, timeout=1
            )
        except subprocess.TimeoutExpired:
            return

        # `as 2 "org.mpris.MediaPlayer2.spotify" "…"`: the active player first.
        words = names.stdout.split('"')
        self.player = (
            words[1].removeprefix("org.mpris.MediaPlayer2.").split(".")[0]
            if names.returncode == 0 and len(words) > 1
            else None
        )
        self.playing = status.returncode == 0 and status.stdout.strip() == "Playing"

    def poll(self) -> None:
        self.poll_levels()
        if time.monotonic() >= self.next_player:
            self.poll_player()
            self.next_player = time.monotonic() + self.PLAYER_SECONDS

    def look(self, context: str, key: SoundKey) -> tuple[int, str]:
        match key.action:
            case Action.VOLUME:
                level = self.levels.get(key.device)
                if level is None:
                    return VolumeLook.IDLE, ""
                if level.muted:
                    return (
                        VolumeLook.MUTED if key.verb == "mute" else VolumeLook.IDLE,
                        "muted",
                    )

                return VolumeLook.IDLE, f"{level.volume}%"

            case Action.MEDIA if key.verb == "toggle":
                return (
                    (MediaLook.PLAYING, "pause")
                    if self.playing
                    else (MediaLook.IDLE, "play")
                )
            case Action.MEDIA if key.verb == "shift":
                return MediaLook.IDLE, self.player or "none"
            case Action.MEDIA:
                return MediaLook.IDLE, ""

            case Action.TALK:
                level = self.levels.get(Device.INPUT)
                if level is None or level.muted:
                    return TalkLook.MUTED, ""

                return TalkLook.LIVE, "live"

        raise ValueError(f"unknown action {key.action}")

    def press(self, context: str, key: SoundKey) -> None:
        match key.action:
            case Action.VOLUME:
                self.spawn([str(self.LAUNCH), self.VOLUME[key.device][key.verb]])
            case Action.MEDIA:
                self.spawn([str(self.LAUNCH), self.MEDIA[key.verb]])
                self.next_player = time.monotonic() + 0.2

    def talk(self, live: bool) -> None:
        source = self.default(Device.INPUT)
        if live:
            self.talk_restore = bool(source.mute)
        self.log.info("push to talk: %s", "live" if live else "released")
        self.connect().source_mute(
            source.index, False if live else bool(self.talk_restore)
        )

    def key_down(self, context: str, key: SoundKey) -> None:
        if key.action != Action.TALK:
            return super().key_down(context, key)

        self.talk(True)

    def key_up(self, context: str, key: SoundKey) -> None:
        if key.action != Action.TALK:
            return super().key_up(context, key)

        self.talk(False)


if __name__ == "__main__":
    command(SoundPlugin)()
