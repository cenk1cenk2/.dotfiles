#!/usr/bin/env -S sh -c 'd="$(dirname "$0")"; uv sync -q --project "$d" && exec "$d/.venv/bin/python" "$0" "$@"'

from __future__ import annotations

import base64
import html
import json
import logging
import os
import re
import socket
import subprocess
import time
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import ClassVar

from deck import Key, Plugin, command


class Session(StrEnum):
    STT = "stt"
    TTS = "tts"


class Action(StrEnum):
    TOGGLE = "dev.kilic.speech.toggle"
    PAUSE = "dev.kilic.speech.pause"
    SEEK = "dev.kilic.speech.seek"
    SESSIONS = "dev.kilic.speech.sessions"


class ToggleLook(IntEnum):
    IDLE = 0
    LIVE = 1
    WORKING = 2
    PAUSED = 3


class PauseLook(IntEnum):
    IDLE = 0
    ACTIVE = 1
    PAUSED = 2


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
    AGENTS_SECONDS = 2.0
    # Process names, as /proc reports them, of the agents the sessions key finds.
    AGENT_NAMES: ClassVar[dict[str, str]] = {
        "claude": "claude",
        "codex": "openai",
        "opencode": "opencode",
    }
    NOTIFY = Path.home() / ".config/wayland/scripts/notify.py"
    ICONS = Path.home() / ".config/opendeck/plugins/dev.kilic.speech.sdPlugin/icons"
    SESSIONS_TILE = "#56b6c2"
    TERMINAL = Path(
        "/usr/share/icons/Tela-yellow-dark/scalable/apps/utilities-terminal.svg"
    )
    KEY = SpeechKey

    log = logging.getLogger("speech-deck")

    def __init__(self, ws):
        super().__init__(ws)
        self.agents: list[dict] = []
        self.next_agents = 0.0
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

    def find_agents(self) -> list[dict]:
        """Every tmux pane with an agent anywhere in its process tree.

        Agents run under wrappers (a profile launcher, a shell, node), so the
        pane's own process is rarely the agent; the whole tree is searched."""
        try:
            listed = subprocess.run(
                [
                    "tmux",
                    "list-panes",
                    "-a",
                    "-F",
                    (
                        "#{pane_id}\t#{pane_pid}\t#{session_name}:#{window_index}"
                        "\t#{pane_current_path}\t#{pane_active}#{window_active}#{session_attached}"
                    ),
                ],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return self.agents
        if listed.returncode != 0:
            return []

        children: dict[int, list[int]] = {}
        names: dict[int, str] = {}
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                stat = (entry / "stat").read_text()
            except OSError:
                continue
            close = stat.rfind(")")
            names[int(entry.name)] = stat[stat.find("(") + 1 : close]
            children.setdefault(int(stat[close + 2 :].split()[1]), []).append(
                int(entry.name)
            )

        agents = []
        for line in listed.stdout.splitlines():
            pane, pid, where, path, flags = (line.split("\t") + [""] * 5)[:5]
            stack = [int(pid)] if pid.isdigit() else []
            while stack:
                current = stack.pop()
                if (vendor := self.AGENT_NAMES.get(names.get(current, ""))) is not None:
                    agents.append(
                        {
                            "pane": pane,
                            "where": where,
                            "project": Path(path).name or "/",
                            "vendor": vendor,
                            "profile": self.profile(current, vendor),
                            "seen": flags[:2] == "11" and flags[2:] not in ("", "0"),
                        }
                    )
                    break
                stack.extend(children.get(current, []))

        return agents

    def profile(self, pid: int, vendor: str) -> str | None:
        """The account an agent runs as, from the config dir its launcher set."""
        try:
            environ = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        except OSError:
            return None
        variable = {"claude": b"CLAUDE_CONFIG_DIR=", "openai": b"CODEX_HOME="}.get(
            vendor
        )
        for item in environ:
            if variable and item.startswith(variable):
                name = Path(item[len(variable) :].decode(errors="replace")).name
                return name.split("-", 1)[1] if "-" in name else name

        return None

    def switch(self, step: int) -> None:
        """Focus the next (or previous) agent after the one on screen."""
        if not self.agents:
            return
        current = next((i for i, a in enumerate(self.agents) if a["seen"]), -1)
        target = self.agents[(current + step) % len(self.agents)]
        self.spawn([str(self.NOTIFY), "focus", "--pane", target["pane"]])
        self.next_agents = time.monotonic() + 0.3

    def next_agent(self) -> tuple[dict, int] | None:
        if not self.agents:
            return None
        current = next((i for i, a in enumerate(self.agents) if a["seen"]), -1)
        index = (current + 1) % len(self.agents)

        return self.agents[index], index

    def image(self, context: str, key: SpeechKey) -> str | None:
        if key.action != Action.SESSIONS:
            return None
        found = self.next_agent()
        if found is None:
            terminal = base64.b64encode(self.TERMINAL.read_bytes()).decode()
            svg = (
                '<svg xmlns="http://www.w3.org/2000/svg" width="144" height="144" viewBox="0 0 144 144">'
                '<rect width="144" height="144" fill="#17191e"/>'
                f'<image href="data:image/svg+xml;base64,{terminal}" x="40" y="52" width="64" height="64"'
                ' opacity="0.3"/></svg>'
            )
            return (
                f"data:image/svg+xml;base64,{base64.b64encode(svg.encode()).decode()}"
            )
        agent, index = found
        mark = re.search(
            r' d="([^"]+)"', (self.ICONS / f"agents/{agent['vendor']}.svg").read_text()
        )
        project = agent["project"]
        if len(project) > 12:
            project = project[:11] + "…"
        size = min(26, round(230 / max(len(project), 1)))
        header = f"{index + 1}/{len(self.agents)}"
        if agent["profile"]:
            header += f" {agent['profile']}"
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="144" height="144" viewBox="0 0 144 144">'
            '<rect width="144" height="144" fill="#17191e"/>'
            f'<rect y="36" width="144" height="108" fill="{self.SESSIONS_TILE}"/>'
            '<text x="72" y="28" font-family="Liberation Sans" font-size="22" font-weight="bold"'
            f' fill="#e5e5e5" text-anchor="middle">{html.escape(header)}</text>'
            + (
                f'<g transform="translate(50 44) scale(1.8333)" fill="#17191e"><path d="{mark[1]}"/></g>'
                if mark
                else ""
            )
            + f'<text x="72" y="130" font-family="Liberation Sans" font-size="{size}" font-weight="bold"'
            f' fill="#17191e" text-anchor="middle">{html.escape(project)}</text></svg>'
        )

        return f"data:image/svg+xml;base64,{base64.b64encode(svg.encode()).decode()}"

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
        if key.action == Action.SESSIONS:
            return 0, ""
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
            case Action.SEEK:
                return 0, ""

        raise ValueError(f"unknown action {key.action}")

    def poll(self) -> None:
        if (
            any(key.action == Action.SESSIONS for key in self.keys.values())
            and time.monotonic() >= self.next_agents
        ):
            self.agents = self.find_agents()
            self.next_agents = time.monotonic() + self.AGENTS_SECONDS
        sessions = {
            key.session for key in self.keys.values() if key.action != Action.SESSIONS
        }
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
            case Action.SEEK:
                self.speech(key.session, "seek", key.settings["seconds"])
            case Action.SESSIONS:
                self.switch(1)

    def holds(self, key: SpeechKey) -> bool:
        return key.action in (Action.TOGGLE, Action.PAUSE, Action.SEEK, Action.SESSIONS)

    def repeats(self, key: SpeechKey) -> bool:
        return key.action == Action.SEEK

    def hold_status(self, key: SpeechKey) -> str | None:
        if key.action == Action.SEEK:
            seconds = float(key.settings["seconds"])
            return f"◀ {-seconds:g}s" if seconds < 0 else f"{seconds:g}s ▶"
        if key.action == Action.SESSIONS:
            return "previous"

        return "kill"

    def hold(self, context: str, key: SpeechKey) -> None:
        if key.action == Action.SEEK:
            return self.press(context, key)
        if key.action == Action.SESSIONS:
            return self.switch(-1)

        self.speech(key.session, "kill")


if __name__ == "__main__":
    command(SpeechPlugin)()
