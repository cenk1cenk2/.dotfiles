#!/usr/bin/env -S sh -c 'd="$(dirname "$0")"; uv sync -q --project "$d" && exec "$d/.venv/bin/python" "$0" "$@"'
"""Pick a coding agent running in tmux via rofi, waiting ones first."""

from __future__ import annotations

import html
import logging
import re
import subprocess
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import click
from dotlib import agents
from dotlib.cli import create_logger, run

from lib import Hyprctl


@dataclass(frozen=True)
class Row:
    search: str
    card: str
    pane: str | None
    entry: dict | None


class AgentMenu:
    log = logging.getLogger("agents")
    NOTIFY = Path.home() / ".config/wayland/scripts/notify.py"
    # The deck's tile colours, so a state reads the same on both.
    URGENT = "#e06c75"
    WAITING = "#d19a66"
    RUNNING = "#56b6c2"
    IDLE = "#abb2bf"
    # Claude Code's session status, as the row names and colours it.
    STATES: ClassVar[dict[str | None, tuple[str, str]]] = {
        "busy": ("working", RUNNING),
        "idle": ("idle", IDLE),
    }
    REPLY_LINES = 4
    LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
    # Cards span several lines, so rows are split on a character no card holds
    # rather than on newlines. Custom key 1 dismisses the selected agent and
    # custom key 2 every waiting one; rofi reports them as exit codes 10 and 11.
    SEPARATOR = "\x1e"
    ROFI_ARGS = (
        "-markup-rows",
        "-format",
        "i",
        "-sep",
        SEPARATOR,
        "-eh",
        str(2 + REPLY_LINES),
        "-theme",
        "hints",
        "-mesg",
        "Enter focus  ·  Ctrl+x dismiss  ·  Ctrl+Shift+x dismiss all",
        "-kb-custom-1",
        "Control+x",
        "-kb-custom-2",
        "Control+Shift+x",
    )
    ROFI_DISMISS = 10
    ROFI_DISMISS_ALL = 11
    # Super+Tab's share of the focused monitor, so both pickers open alike.
    WIDTH = 0.75
    HEIGHT = 0.85
    # Logical px of one card line at the theme's font, what a card spends on
    # padding and spacing beyond its lines, and what the window spends on the
    # search bar, the hint strip and its own padding.
    LINE_PX = 30
    CARD_CHROME = 34
    WINDOW_CHROME = 190
    # rofi cuts a row line short with an ellipsis rather than wrapping it, so
    # replies are wrapped here to the card's width: the theme's window is
    # fullscreen, which makes that the monitor's width less the padding around
    # the text, at an average italic character width.
    TEXT_INSET = 160
    CHAR_PX = 10.5

    def __init__(self) -> None:
        monitor = Hyprctl().focused_monitor() or {}
        scale = monitor.get("scale", 1) or 1
        self.width = monitor.get("width", 1920) / scale
        self.height = monitor.get("height", 1080) / scale
        if monitor.get("transform", 0) % 2:
            self.width, self.height = self.height, self.width
        self.reply_width = int((self.width - self.TEXT_INSET) / self.CHAR_PX)

    def run(self) -> None:
        # Dismissing reopens the list, so a queue can be cleared in one go.
        while rows := self.rows():
            choice, code = self.pick(rows)
            if choice is None:
                return
            row = rows[choice]
            match code:
                case self.ROFI_DISMISS:
                    self.dismiss(row.entry)
                case self.ROFI_DISMISS_ALL:
                    agents.edit_waiting(lambda w: [])
                case _:
                    self.dismiss(row.entry)
                    if row.pane:
                        run(
                            [str(self.NOTIFY), "focus", "--pane", row.pane],
                            log=self.log,
                            timeout=10.0,
                        )
                    return
        self.log.info("no agents running")

    @staticmethod
    def dismiss(entry: dict | None) -> None:
        if entry is not None:
            agents.edit_waiting(lambda w: [e for e in w if e != entry])

    def rows(self) -> list[Row]:
        panes = agents.tmux_panes() or {}
        waiting = sorted(
            agents.edit_waiting(lambda w: [e for e in w if not agents.stale(e, panes)]),
            key=lambda e: (not e.get("urgent"), e.get("at", 0)),
        )
        found = agents.walk(panes)
        flagged = {e.get("pane") for e in waiting}

        return [
            *(
                self.waiting_row(e, panes.get(e.get("pane")), found.get(e.get("pane")))
                for e in waiting
            ),
            *(
                self.running_row(agent, panes[pane])
                for pane, agent in found.items()
                if pane not in flagged
            ),
        ]

    def pick(self, rows: list[Row]) -> tuple[int | None, int]:
        cmd = ["rofi", "-dmenu", "-i", "-p", "Agents", *self.ROFI_ARGS, *self.size()]
        self.log.debug("spawn: %s", " ".join(cmd))
        proc = subprocess.run(
            cmd,
            # The card is only shown: rofi filters on the row's own text, so a
            # query matches the metadata and never the reply.
            input=self.SEPARATOR.join(f"{r.search}\0display\x1f{r.card}" for r in rows),
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.stderr:
            self.log.debug("rofi stderr: %s", proc.stderr.strip())
        if proc.returncode not in (0, self.ROFI_DISMISS, self.ROFI_DISMISS_ALL):
            return None, proc.returncode
        try:
            return int(proc.stdout.strip()), proc.returncode
        except ValueError:
            return None, proc.returncode

    def size(self) -> list[str]:
        """Fit the window to the focused monitor as Super+Tab does, holding as
        many whole cards as its height allows."""
        card = (2 + self.REPLY_LINES) * self.LINE_PX + self.CARD_CHROME
        lines = max(1, int((self.height * self.HEIGHT - self.WINDOW_CHROME) // card))

        return [
            "-theme-str",
            (
                f"window {{ width: {int(self.width * self.WIDTH)}px;"
                f" height: {lines * card + self.WINDOW_CHROME}px; }}"
            ),
            "-theme-str",
            f"listview {{ lines: {lines}; }}",
            "-theme-str",
            "mainbox { padding: 1em; }",
        ]

    def waiting_row(self, entry: dict, pane: dict | None, agent: dict | None) -> Row:
        session = self.session(agent)
        project = entry.get("directory") or "?"
        card = "\n".join(
            (
                self.headline(
                    "waiting",
                    self.URGENT if entry.get("urgent") else self.WAITING,
                    session.get("name"),
                    entry.get("profile"),
                    project,
                    f"{entry.get('message') or ''}, {self.age(entry.get('at'))} ago",
                ),
                self.location(entry.get("pane"), pane),
                self.reply(self.last_reply(agent, session) or entry.get("context")),
            )
        )
        search = self.search(
            "waiting",
            session.get("name"),
            entry.get("profile"),
            project,
            entry.get("vendor"),
            entry.get("pane"),
            pane,
        )

        return Row(search, card, entry.get("pane"), entry)

    def running_row(self, agent: dict, pane: dict) -> Row:
        session = self.session(agent)
        state, colour = self.STATES.get(
            session.get("status"), ("running", self.RUNNING)
        )
        project = Path(pane["path"]).name or "/"
        card = "\n".join(
            (
                self.headline(
                    state,
                    colour,
                    session.get("name"),
                    agent["profile"],
                    project,
                    agent["name"] + (", on screen" if pane["seen"] else ""),
                ),
                self.location(agent["pane"], pane),
                self.reply(self.last_reply(agent, session)),
            )
        )
        search = self.search(
            state,
            session.get("name"),
            agent["profile"],
            project,
            agent["name"],
            agent["pane"],
            pane,
        )

        return Row(search, card, agent["pane"], None)

    @staticmethod
    def search(
        state: str,
        name: str | None,
        profile: str | None,
        project: str,
        vendor: str | None,
        pane_id: str | None,
        pane: dict | None,
    ) -> str:
        where = (pane["where"], pane["path"]) if pane else ()

        return " ".join(
            filter(None, (state, name, profile, project, vendor, pane_id, *where))
        )

    @staticmethod
    def session(agent: dict | None) -> dict:
        """Claude Code's own record of the session; codex keeps none by pid."""
        if agent is None or agent["vendor"] != "claude" or agent["home"] is None:
            return {}

        return agents.claude_session(agent["home"], agent["pid"]) or {}

    @staticmethod
    def last_reply(agent: dict | None, session: dict) -> str:
        if agent is None or not session.get("sessionId"):
            return ""
        transcript = agents.claude_transcript(agent["home"], session["sessionId"])

        return agents.last_reply(transcript) if transcript else ""

    def reply(self, text: str | None) -> str:
        """The opening lines of the reply, wrapped, with its own line breaks
        kept and blank lines dropped. Markdown is read as plain text, so link
        targets and emphasis markers are stripped rather than shown."""
        plain = self.LINK.sub(r"\1", text or "").replace("**", "").replace("`", "")
        lines = [
            line
            for paragraph in plain.splitlines()
            for line in textwrap.wrap(paragraph, self.reply_width)
        ]
        if not lines:
            return '<span alpha="60%">no reply yet</span>'
        shown = lines[: self.REPLY_LINES]
        if len(lines) > self.REPLY_LINES:
            shown[-1] = textwrap.shorten(
                shown[-1] + " …", self.reply_width - 2, placeholder=" …"
            )

        return "\n".join(f"<i>{html.escape(line, quote=False)}</i>" for line in shown)

    @staticmethod
    def headline(
        state: str,
        colour: str,
        name: str | None,
        profile: str | None,
        project: str,
        note: str,
    ) -> str:
        title = html.escape(name or project, quote=False)
        account = html.escape(
            f"{profile} · {project}" if profile else project, quote=False
        )

        return (
            f'<span foreground="{colour}"><b>{state}</b></span>  <b>{title}</b>  '
            f"{account}  "
            f'<span alpha="60%">{html.escape(note, quote=False)}</span>'
        )

    @staticmethod
    def location(pane_id: str | None, pane: dict | None) -> str:
        if pane is None:
            return '<span alpha="60%">no tmux pane</span>'
        path = pane["path"].replace(str(Path.home()), "~", 1)

        return (
            f'<span alpha="60%">{html.escape(pane["where"], quote=False)}'
            f"  {pane_id}  {html.escape(path, quote=False)}</span>"
        )

    @staticmethod
    def age(at: float | None) -> str:
        seconds = int(time.time() - (at or time.time()))
        if seconds < 60:
            return f"{seconds}s"
        if seconds < 3600:
            return f"{seconds // 60}m"

        return f"{seconds // 3600}h"


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("-v", "--verbose", is_flag=True, help="Show subprocess traces.")
def cmd_main(verbose: bool) -> None:
    create_logger(verbose, name="agents")
    AgentMenu().run()


AgentMenu.cli = cmd_main

if __name__ == "__main__":
    AgentMenu.cli()
