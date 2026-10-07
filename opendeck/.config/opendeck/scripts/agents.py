#!/usr/bin/env -S sh -c 'd="$(dirname "$0")"; uv sync -q --project "$d" && exec "$d/.venv/bin/python" "$0" "$@"'

from __future__ import annotations

import base64
import fcntl
import html
import json
import logging
import os
import re
import subprocess
import time
from enum import StrEnum
from pathlib import Path
from typing import ClassVar

from deck import Key, Plugin, command


class Action(StrEnum):
    WAITING = "dev.kilic.agents.waiting"
    SESSIONS = "dev.kilic.agents.sessions"


class AgentKey(Key):
    pass


class AgentsPlugin(Plugin):
    """Stream Deck plugin for coding agents running in tmux.

    `waiting` keys show the agents the notify hook flagged: the left one the
    selected agent, the right one the whole queue. `sessions` finds every
    agent running in a tmux pane, flagged or not, and cycles through them."""

    # Written by the agents' notify hook, one entry per tmux pane.
    WAITING = (
        Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
        / "agents-waiting.json"
    )
    NOTIFY = Path.home() / ".config/wayland/scripts/notify.py"
    ICONS = Path.home() / ".config/opendeck/plugins/dev.kilic.agents.sdPlugin/icons"
    TERMINAL = Path(
        "/usr/share/icons/Tela-yellow-dark/scalable/apps/utilities-terminal.svg"
    )
    # An entry with no tmux pane can be neither checked nor focused.
    PANELESS_SECONDS = 30 * 60
    SAMPLE_SECONDS = 1.0
    AGENTS_SECONDS = 2.0
    WALK_SECONDS = 15.0
    URGENT = "#e06c75"
    WAITING_TILE = "#d19a66"
    SESSIONS_TILE = "#56b6c2"
    # Simple Icons marks (CC0) in icons/agents/, by hook vendor.
    VENDORS: ClassVar[dict[str, str]] = {
        "Claude Code": "claude",
        "Codex": "openai",
        "OpenCode": "opencode",
    }
    # Process names, as /proc reports them, of the agents `sessions` finds.
    AGENT_NAMES: ClassVar[dict[str, str]] = {
        "claude": "claude",
        "codex": "openai",
        "opencode": "opencode",
    }
    KEY = AgentKey

    log = logging.getLogger("agents-deck")

    def __init__(self, ws):
        super().__init__(ws)
        self.waiting: list[dict] = []
        # The waiting agent the left key shows, by pane and alarm time; the
        # right key moves it along the queue.
        self.selected: tuple | None = None
        self.agents: list[dict] = []
        self.panes: dict[str, dict] | None = None
        self.panes_at = 0.0
        self.found: dict[str, dict] = {}
        self.walked_pids: set[int] = set()
        self.walked_at = 0.0
        self.waiting_changed: int | None = None
        self.last_seen: str | None = None
        self.next_sample = 0.0
        self.next_agents = 0.0

    def query(self, cmd: list[str]) -> subprocess.CompletedProcess:
        """A status read, traced at DEBUG since it runs every second."""
        self.log.debug("spawn: %s", " ".join(cmd))
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=2, check=False
        )
        if proc.stderr:
            self.log.debug("%s stderr: %s", cmd[0], proc.stderr.strip())

        return proc

    def poll(self) -> None:
        actions = {key.action for key in self.keys.values()}
        now = time.monotonic()
        if Action.WAITING in actions and now >= self.next_sample:
            self.sample_waiting()
            self.next_sample = now + self.SAMPLE_SECONDS
        if Action.SESSIONS in actions and now >= self.next_agents:
            self.agents = self.find_agents()
            self.next_agents = now + self.AGENTS_SECONDS

    @staticmethod
    def uri(svg: str) -> str:
        return f"data:image/svg+xml;base64,{base64.b64encode(svg.encode()).decode()}"

    def image(self, context: str, key: AgentKey) -> str | None:
        match key.action:
            case Action.WAITING:
                return self.agent_image(key)
            case Action.SESSIONS:
                return self.sessions_image()

        return None

    def look(self, context: str, key: AgentKey) -> tuple[int, str]:
        return 0, ""

    def press(self, context: str, key: AgentKey) -> None:
        match key.action:
            case Action.SESSIONS:
                self.switch(1)
            case Action.WAITING if key.settings["slot"] == 1:
                self.cycle()
            case Action.WAITING if (entry := self.agent(key)) is not None:
                if entry.get("pane"):
                    self.spawn([str(self.NOTIFY), "focus", "--pane", entry["pane"]])
                self.dismiss(entry)

    def holds(self, key: AgentKey) -> bool:
        return True

    def hold_status(self, key: AgentKey) -> str | None:
        if key.action == Action.SESSIONS:
            return "previous"

        return "dismiss all" if key.settings["slot"] == 1 else "dismiss"

    def hold(self, context: str, key: AgentKey) -> None:
        if key.action == Action.SESSIONS:
            return self.switch(-1)
        if key.settings["slot"] == 1:
            self.edit_waiting(lambda w: False)
            self.render()
        elif (entry := self.agent(key)) is not None:
            self.dismiss(entry)

    def find_agents(self) -> list[dict]:
        """Every tmux pane with an agent anywhere in its process tree.

        Agents run under wrappers (a profile launcher, a shell, node), so the
        pane's own process is rarely the agent and the whole tree is searched.
        That walk reads all of /proc, so it runs only when the panes change or
        every WALK_SECONDS, to catch an agent started in an existing pane; in
        between, the agents already found are only checked to be alive."""
        panes = self.tmux_panes()
        if panes is None:
            return self.agents

        pids = {pane["pid"] for pane in panes.values()}
        now = time.monotonic()
        if pids != self.walked_pids or now - self.walked_at >= self.WALK_SECONDS:
            self.found = self.walk(panes)
            self.walked_pids, self.walked_at = pids, now
        else:
            self.found = {
                pane: agent
                for pane, agent in self.found.items()
                if pane in panes and self.alive(agent["pid"], agent["name"])
            }

        return [
            {
                **agent,
                "where": panes[pane]["where"],
                "project": Path(panes[pane]["path"]).name or "/",
                "seen": panes[pane]["seen"],
            }
            for pane, agent in self.found.items()
            if pane in panes
        ]

    def walk(self, panes: dict[str, dict]) -> dict[str, dict]:
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

        found = {}
        for pane, info in panes.items():
            stack = [info["pid"]]
            while stack:
                current = stack.pop()
                name = names.get(current, "")
                if (vendor := self.AGENT_NAMES.get(name)) is not None:
                    found[pane] = {
                        "pane": pane,
                        "pid": current,
                        "name": name,
                        "vendor": vendor,
                        "profile": self.profile(current, vendor),
                    }
                    break
                stack.extend(children.get(current, []))

        return found

    @staticmethod
    def alive(pid: int, name: str) -> bool:
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            return False

        return stat[stat.find("(") + 1 : stat.rfind(")")] == name

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

    def current_agent(self) -> tuple[dict, int] | None:
        """The agent on screen, else the one last on screen, else the first.

        Remembering the last one keeps the key steady while another app has
        focus, instead of falling back to tmux's order."""
        if not self.agents:
            return None
        for index, agent in enumerate(self.agents):
            if agent["seen"]:
                self.last_seen = agent["pane"]
                return agent, index
        for index, agent in enumerate(self.agents):
            if agent["pane"] == self.last_seen:
                return agent, index

        return self.agents[0], 0

    def tmux_panes(self) -> dict[str, dict] | None:
        """Every tmux pane, from one `list-panes` shared by both key kinds
        within a poll; None without tmux.

        On screen means the active pane of its session's active window, in a
        session some client is attached to. Kitty's own focus is not asked,
        which would cost a `kitty @ ls` every poll."""
        if time.monotonic() - self.panes_at < self.SAMPLE_SECONDS / 2:
            return self.panes
        try:
            proc = self.query(
                [
                    "tmux",
                    "list-panes",
                    "-a",
                    "-F",
                    (
                        "#{pane_id}\t#{pane_pid}\t#{session_name}:#{window_index}"
                        "\t#{pane_current_path}\t#{pane_active}#{window_active}#{session_attached}"
                    ),
                ]
            )
        except subprocess.TimeoutExpired:
            return None
        self.panes_at = time.monotonic()
        if proc.returncode != 0:
            self.panes = None
            return None

        self.panes = {}
        for line in proc.stdout.splitlines():
            pane, pid, where, path, flags = (line.split("\t") + [""] * 5)[:5]
            if pid.isdigit():
                self.panes[pane] = {
                    "pid": int(pid),
                    "where": where,
                    "path": path,
                    "seen": flags[:2] == "11" and flags[2:] not in ("", "0"),
                }

        return self.panes

    def edit_waiting(self, keep) -> None:
        """Rewrite the waiting list under the hook's lock, keeping what `keep` says."""
        with open(self.WAITING.with_suffix(".lock"), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                waiting = json.loads(self.WAITING.read_text())
            except OSError, ValueError:
                waiting = []
            kept = [w for w in waiting if keep(w)]
            if kept != waiting:
                tmp = self.WAITING.with_suffix(".tmp")
                tmp.write_text(json.dumps(kept))
                tmp.replace(self.WAITING)
        self.waiting = sorted(kept, key=lambda w: (not w.get("urgent"), w.get("at", 0)))

    def sample_waiting(self) -> None:
        """Re-read the hook's list when it changed, and prune it while it has
        entries; with nothing waiting this costs one stat and no process."""
        try:
            changed = self.WAITING.stat().st_mtime_ns
        except OSError:
            self.waiting = []
            return
        if changed == self.waiting_changed and not self.waiting:
            return

        panes = (
            self.tmux_panes()
            if self.waiting or changed != self.waiting_changed
            else None
        )

        def keep(entry: dict) -> bool:
            if not entry.get("pane"):
                return time.time() - entry.get("at", 0) < self.PANELESS_SECONDS
            if panes is None:
                return True
            info = panes.get(entry["pane"])

            return info is not None and not info["seen"]

        self.edit_waiting(keep)
        try:
            self.waiting_changed = self.WAITING.stat().st_mtime_ns
        except OSError:
            self.waiting_changed = None

    @staticmethod
    def identity(entry: dict) -> tuple:
        return entry.get("pane"), entry.get("at")

    def agent(self, key: AgentKey) -> dict | None:
        """The selected agent for the left key; the right key shows the queue."""
        if key.settings["slot"] != 0 or not self.waiting:
            return None

        return next(
            (w for w in self.waiting if self.identity(w) == self.selected),
            self.waiting[0],
        )

    def terminal(self) -> str:
        return base64.b64encode(self.TERMINAL.read_bytes()).decode()

    def mark(self, entry: dict) -> str | None:
        vendor = self.VENDORS.get(entry.get("vendor", ""), "claude")
        found = re.search(
            r' d="([^"]+)"', (self.ICONS / f"agents/{vendor}.svg").read_text()
        )

        return found[1] if found else None

    def queue_image(self) -> str:
        """The whole queue in brief: a count, then a mark and project per row.

        The row the left key shows is banded, and the three rows drawn always
        include it, so cycling past the third agent scrolls the list."""
        waiting = self.waiting
        if not waiting:
            return self.uri(
                '<svg xmlns="http://www.w3.org/2000/svg" width="144" height="144" viewBox="0 0 144 144">'
                '<rect width="144" height="144" fill="#17191e"/>'
                # Each card blanks the key behind it before drawing, so the
                # stack reads as solid cards rather than overlapping ghosts.
                + "".join(
                    f'<rect x="{x + 4}" y="{y + 6}" width="48" height="44" rx="6" fill="#17191e"/>'
                    f'<image href="data:image/svg+xml;base64,{self.terminal()}"'
                    f' x="{x}" y="{y}" width="56" height="56" opacity="{opacity}"/>'
                    for x, y, opacity in ((60, 38, 0.12), (50, 50, 0.2), (40, 62, 0.3))
                )
                + "</svg>"
            )

        colour = (
            self.URGENT if any(w.get("urgent") for w in waiting) else self.WAITING_TILE
        )
        selected = self.agent(AgentKey(Action.WAITING, {"slot": 0}))
        index = waiting.index(selected) if selected in waiting else 0
        start = max(0, min(index - 1, len(waiting) - 3))
        rows = ""
        for i, entry in enumerate(waiting[start : start + 3]):
            y = 44 + i * 33
            if entry is selected:
                rows += f'<rect y="{y - 4}" width="144" height="33" fill="#17191e" fill-opacity="0.22"/>'
            project = entry.get("directory") or "?"
            if len(project) > 8:
                project = project[:7] + "…"
            if mark := self.mark(entry):
                rows += (
                    f'<g transform="translate(8 {y}) scale(1.1667)" fill="#17191e">'
                    f'<path d="{mark}"/></g>'
                )
            rows += (
                f'<text x="40" y="{y + 22}" font-family="Liberation Sans" font-size="22"'
                f' font-weight="bold" fill="#17191e">{html.escape(project)}</text>'
            )

        return self.uri(
            '<svg xmlns="http://www.w3.org/2000/svg" width="144" height="144" viewBox="0 0 144 144">'
            '<rect width="144" height="144" fill="#17191e"/>'
            f'<rect y="36" width="144" height="108" fill="{colour}"/>'
            '<text x="72" y="28" font-family="Liberation Sans" font-size="24" font-weight="bold"'
            f' fill="#e5e5e5" text-anchor="middle">{len(waiting)} waiting</text>{rows}</svg>'
        )

    def agent_image(self, key: AgentKey) -> str:
        """The waiting agent's mark over its project name, in big type.

        A dark key with a faint terminal while nothing waits in this slot."""
        if key.settings["slot"] == 1:
            return self.queue_image()

        entry = self.agent(key)
        if entry is None:
            icon = base64.b64encode(self.TERMINAL.read_bytes()).decode()
            return self.uri(
                '<svg xmlns="http://www.w3.org/2000/svg" width="144" height="144" viewBox="0 0 144 144">'
                '<rect width="144" height="144" fill="#17191e"/>'
                f'<image href="data:image/svg+xml;base64,{icon}" x="40" y="52" width="64" height="64"'
                ' opacity="0.3"/></svg>'
            )

        colour = self.URGENT if entry.get("urgent") else self.WAITING_TILE
        mark = self.mark(entry)
        project = entry.get("directory") or "?"
        if len(project) > 12:
            project = project[:11] + "…"
        size = min(26, round(230 / max(len(project), 1)))
        profile = html.escape(entry.get("profile") or "")

        return self.uri(
            '<svg xmlns="http://www.w3.org/2000/svg" width="144" height="144" viewBox="0 0 144 144">'
            '<rect width="144" height="144" fill="#17191e"/>'
            f'<rect y="36" width="144" height="108" fill="{colour}"/>'
            '<text x="72" y="28" font-family="Liberation Sans" font-size="24"'
            f' font-weight="bold" fill="#e5e5e5" text-anchor="middle">{profile}</text>'
            + (
                f'<g transform="translate(50 44) scale(1.8333)" fill="#17191e"><path d="{mark}"/></g>'
                if mark
                else ""
            )
            + f'<text x="72" y="130" font-family="Liberation Sans" font-size="{size}" font-weight="bold"'
            f' fill="#17191e" text-anchor="middle">{html.escape(project)}</text></svg>'
        )

    def sessions_image(self) -> str:
        found = self.current_agent()
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

    def cycle(self) -> None:
        """Select the next waiting agent after the current one, wrapping."""
        if not self.waiting:
            return
        current = self.agent(AgentKey(Action.WAITING, {"slot": 0}))
        index = self.waiting.index(current) if current in self.waiting else -1
        self.selected = self.identity(self.waiting[(index + 1) % len(self.waiting)])
        self.render()

    def dismiss(self, entry: dict) -> None:
        self.edit_waiting(lambda w: w != entry)
        self.render()


if __name__ == "__main__":
    command(AgentsPlugin)()
