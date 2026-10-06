#!/usr/bin/env -S sh -c 'd="$(dirname "$0")"; uv sync -q --project "$d" && exec "$d/.venv/bin/python" "$0" "$@"'

from __future__ import annotations

import base64
import io
import logging
import re
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import ClassVar

import pulsectl
from PIL import Image, ImageOps

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
    # Holding prev or next seeks the active player by this much instead.
    SEEK: ClassVar[dict[str, str]] = {"prev": "5-", "next": "5+"}
    MUTED = "#e06c75"
    PLAYING = "#98c379"
    PAUSED = "#e5c07b"
    # The tile every icon here draws its glyph on.
    TILE = re.compile(r'<rect y="36" width="144" height="108" fill="(#[0-9a-f]{6})"/>')
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
        self.position = 0.0
        self.length = 0.0
        self.position_at = 0.0
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
                    (
                        "{{status}}\t{{artist}}\t{{title}}\t{{mpris:artUrl}}"
                        "\t{{position}}\t{{mpris:length}}"
                    ),
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
        status, artist, title, art_url, position, length = (
            metadata.stdout.rstrip("\n").split("\t")
            if metadata.returncode == 0 and metadata.stdout.count("\t") == 5
            else ("", "", "", "", "", "")
        )
        self.playing = status == "Playing"
        # MPRIS counts both in microseconds.
        self.position = int(position or 0) / 1e6
        self.length = int(length or 0) / 1e6
        self.position_at = time.monotonic()
        self.track = " - ".join(part for part in (artist, title) if part)
        if (art_url or None) != self.art_url:
            self.art_url = art_url or None
            self.art = self.fetch_art(self.art_url) if self.art_url else None

    def fetch_art(self, url: str) -> str | None:
        """The cover at `url` as a key-sized JPEG data URI; None when unreadable.

        Downscaled once per track, since the key is redrawn every time the
        progress bar moves and a player's cover is often 640px."""
        self.log.debug("art: %s", url)
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                cover = Image.open(io.BytesIO(response.read())).convert("RGB")
        except (urllib.error.URLError, OSError, ValueError) as e:
            self.log.debug("art unavailable: %s", e)
            return None

        out = io.BytesIO()
        ImageOps.fit(cover, (144, 144)).save(out, "JPEG", quality=85)

        return f"data:image/jpeg;base64,{base64.b64encode(out.getvalue()).decode()}"

    @staticmethod
    def uri(svg: str) -> str:
        return f"data:image/svg+xml;base64,{base64.b64encode(svg.encode()).decode()}"

    def progress(self) -> float:
        if not self.length:
            return 0.0
        elapsed = time.monotonic() - self.position_at if self.playing else 0.0

        return min(1.0, (self.position + elapsed) / self.length)

    def gauge(self, key: SoundKey, level: Level) -> str:
        """The key's icon with its tile filled up to the level, or red when muted."""
        svg = (self.ICONS / f"{key.device}-{key.verb}.svg").read_text()
        if level.muted:
            return self.uri(
                self.TILE.sub(
                    f'<rect y="36" width="144" height="108" fill="{self.MUTED}"/>', svg
                )
            )

        height = round(108 * min(level.volume, 100) / 100)

        return self.uri(
            self.TILE.sub(
                rf'<rect y="36" width="144" height="108" fill="\1" fill-opacity="0.35"/>'
                rf'<rect y="{144 - height}" width="144" height="{height}" fill="\1"/>',
                svg,
            )
        )

    def marquee(self, text: str) -> str:
        if len(text) <= self.MARQUEE_WIDTH:
            return text

        loop = text + "   "
        start = int(time.monotonic() / self.MARQUEE_SECONDS) % len(loop)

        return (loop + loop)[start : start + self.MARQUEE_WIDTH]

    def image(self, context: str, key: SoundKey) -> str | None:
        if key.action == Action.VOLUME:
            level = self.levels.get(key.device)
            return self.gauge(key, level) if level else None
        if key.action != Action.MEDIA or key.verb != "toggle":
            return None

        colour = self.PLAYING if self.playing else self.PAUSED
        # A dark track with the elapsed part filled inside it, the same on the
        # cover and on the plain icon.
        bar = (
            '<rect y="124" width="144" height="20" fill="#17191e"/>'
            f'<rect x="4" y="128" width="{round(136 * self.progress())}" height="12"'
            f' rx="3" fill="{colour}"/>'
            if self.length
            else ""
        )
        if self.art is None:
            icon = (
                self.ICONS / ("pause.svg" if self.playing else "play.svg")
            ).read_text()
            return self.uri(icon.replace("</svg>", bar + "</svg>"))

        return self.uri(
            '<svg xmlns="http://www.w3.org/2000/svg" width="144" height="144" viewBox="0 0 144 144">'
            f'<image href="{self.art}" width="144" height="144" preserveAspectRatio="xMidYMid slice"/>'
            + (
                ""
                if self.playing
                else '<rect width="144" height="144" fill="#17191e" fill-opacity="0.55"/>'
            )
            + '<rect width="144" height="36" fill="#17191e" fill-opacity="0.85"/>'
            f"{bar}"
            f'<rect x="3" y="3" width="138" height="138" fill="none" stroke="{colour}" stroke-width="6"/>'
            f'<circle cx="118" cy="98" r="20" fill="{colour}"/>'
            '<g transform="translate(105 85) scale(1.625)" fill="#17191e">'
            f'<path d="{self.PAUSE if self.playing else self.PLAY}"/></g></svg>'
        )

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

    def holds(self, key: SoundKey) -> bool:
        return key.action == Action.MEDIA and key.verb in self.SEEK

    def repeats(self, key: SoundKey) -> bool:
        return self.holds(key)

    def hold(self, context: str, key: SoundKey) -> None:
        self.spawn(["playerctl", "-p", "playerctld", "position", self.SEEK[key.verb]])
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
