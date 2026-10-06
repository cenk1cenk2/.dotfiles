#!/usr/bin/env -S sh -c 'exec uv run --project "$(dirname "$0")" "$0" "$@"'

from __future__ import annotations

import base64
import logging
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import ClassVar

import pulsectl

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
    # Glyphs of the play key, drawn over the cover art as a corner badge.
    PLAY = "M 3,2 V 14 L 14,8 Z"
    PAUSE = "M 2 2 L 2 14 L 6 14 L 6 2 L 2 2 z M 10 2 L 10 14 L 14 14 L 14 2 L 10 2 z"
    ICONS = Path.home() / ".config/opendeck/plugins/dev.kilic.sound.sdPlugin/icons"
    # Characters of track that fit across a key, and how fast a longer one
    # scrolls through that window.
    MARQUEE_WIDTH = 11
    MARQUEE_SECONDS = 0.4
    KEY = SoundKey

    log = logging.getLogger("sound-deck")

    def __init__(self, ws):
        super().__init__(ws)
        self.pulse: pulsectl.Pulse | None = None
        self.levels: dict[Device, Level] = {}
        self.player: str | None = None
        self.playing = False
        self.track = ""
        self.art_url: str | None = None
        self.art: str | None = None
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

    def query(self, cmd: list[str]) -> subprocess.CompletedProcess:
        """A status read, traced at DEBUG since it runs every second."""
        self.log.debug("spawn: %s", " ".join(cmd))
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=1, check=False
        )
        if proc.stderr:
            self.log.debug("%s stderr: %s", cmd[0], proc.stderr.strip())

        return proc

    def poll_player(self) -> None:
        try:
            names = self.query(
                [
                    "busctl",
                    "--user",
                    "get-property",
                    "org.mpris.MediaPlayer2.playerctld",
                    "/org/mpris/MediaPlayer2",
                    "com.github.altdesktop.playerctld",
                    "PlayerNames",
                ]
            )
            metadata = self.query(
                [
                    "playerctl",
                    "-p",
                    "playerctld",
                    "metadata",
                    "--format",
                    "{{status}}\t{{artist}}\t{{title}}\t{{mpris:artUrl}}",
                ]
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
        status, artist, title, art_url = (
            metadata.stdout.rstrip("\n").split("\t")
            if metadata.returncode == 0 and metadata.stdout.count("\t") == 3
            else ("", "", "", "")
        )
        self.playing = status == "Playing"
        self.track = " - ".join(part for part in (artist, title) if part)
        if (art_url or None) != self.art_url:
            self.art_url = art_url or None
            self.art = self.fetch_art(self.art_url) if self.art_url else None

    def fetch_art(self, url: str) -> str | None:
        """The cover at `url` as a data URI; None when it cannot be read."""
        self.log.debug("art: %s", url)
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                mime = response.headers.get_content_type()
                if mime == "application/octet-stream":
                    mime = "image/jpeg"
                data = response.read()
        except (urllib.error.URLError, OSError, ValueError) as e:
            self.log.debug("art unavailable: %s", e)
            return None

        return f"data:{mime};base64,{base64.b64encode(data).decode()}"

    def marquee(self, text: str) -> str:
        if len(text) <= self.MARQUEE_WIDTH:
            return text

        loop = text + "   "
        start = int(time.monotonic() / self.MARQUEE_SECONDS) % len(loop)

        return (loop + loop)[start : start + self.MARQUEE_WIDTH]

    def image(self, context: str, key: SoundKey) -> str | None:
        if key.action != Action.MEDIA or key.verb != "toggle":
            return None
        if self.art is None:
            icon = self.ICONS / ("pause.svg" if self.playing else "play.svg")
            return f"data:image/svg+xml;base64,{base64.b64encode(icon.read_bytes()).decode()}"

        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="144" height="144" viewBox="0 0 144 144">'
            f'<image href="{self.art}" width="144" height="144" preserveAspectRatio="xMidYMid slice"/>'
            '<rect width="144" height="36" fill="#17191e" fill-opacity="0.85"/>'
            '<circle cx="118" cy="118" r="22" fill="#98c379"/>'
            '<g transform="translate(104 104) scale(1.75)" fill="#17191e">'
            f'<path d="{self.PAUSE if self.playing else self.PLAY}"/></g></svg>'
        )

        return f"data:image/svg+xml;base64,{base64.b64encode(svg.encode()).decode()}"

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
                look = MediaLook.PLAYING if self.playing else MediaLook.IDLE
                if self.track:
                    return look, self.marquee(self.track)

                return look, "pause" if self.playing else "play"
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
