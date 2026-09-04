#!/usr/bin/env -S sh -c 'exec uv run --project "$(dirname "$0")" "$0" "$@"'

from __future__ import annotations

import glob
import json
import logging
import os
import sys
import textwrap
from pathlib import Path

import click
from dotlib.cli import create_logger, run
from dotlib.notify import (
    Chime,
    ChimeDirection,
    Notification,
    NotifyChannel,
    Urgency,
    bell,
)


class Notify:
    """Claude Code Notification hook: desktop popup with context, plus a chime.

    Clicking the popup focuses the pane this hook was spawned in: the right
    tmux window on the right client, and the kitty window hosting it."""

    # The transcript is JSONL and can run to megabytes; the last assistant
    # message is always inside the final few entries.
    TAIL_BYTES = 64 << 10
    CONTEXT_CHARS = 300
    # Notification messages that must not be missed while glancing away.
    URGENT_WORDS = ("permission", "approval", "waiting for your input")
    # Sent with every popup: an explicit expire-timeout wins over the server's
    # per-urgency defaults, so this script decides how long its own popups live.
    SHOW_MS = 5000
    RUN_TIMEOUT = 3.0

    log = logging.getLogger("notify")

    cli = click.Group()

    @staticmethod
    @cli.command("send")
    @click.argument("profile", required=False)
    @click.option("--verbose", "-v", is_flag=True, help="Debug logging.")
    def cmd_send(profile: str | None, verbose: bool) -> None:
        """Read the hook payload from stdin and raise the alarm."""
        create_logger(verbose)

        try:
            payload = json.load(sys.stdin)
        except json.JSONDecodeError as e:
            Notify.log.warning("bad hook payload: %s", e)
            payload = {}

        directory = Path(payload.get("cwd") or "/").name or "/"
        message = payload.get("message") or "Waiting for input"

        urgency = Urgency.NORMAL
        if any(word in message.lower() for word in Notify.URGENT_WORDS):
            urgency = Urgency.CRITICAL

        body = message
        if context := Notify.context(payload.get("transcript_path")):
            body += f"\n\n{context}"

        label = f" ({profile})" if profile else ""
        # Chime first: the popup call blocks for its lifetime waiting on a
        # click, and the sound belongs to its appearance.
        Chime(ChimeDirection.UP).play()
        # Marks the tmux window and the kitty tab, which the popup cannot do
        # for a session the user is not currently looking at.
        bell()
        clicked = Notification(
            f"Claude Code{label} — {directory}",
            icon="utilities-terminal",
            channel=NotifyChannel.DESKTOP,
        ).send(
            body,
            timeout=Notify.SHOW_MS,
            urgency=urgency,
            actions=[("default", "Focus")],
        )
        if clicked:
            Notify.focus()

    @staticmethod
    @cli.command("focus")
    @click.option("--verbose", "-v", is_flag=True, help="Debug logging.")
    def cmd_focus(verbose: bool) -> None:
        """Jump to this pane without a popup, for testing the chain."""
        create_logger(verbose)
        Notify.focus()

    @classmethod
    def context(cls, transcript: str | None) -> str:
        """The last assistant text in the transcript.

        The hook's own message is generic ("needs your permission"); what
        Claude was saying when it stopped is the part worth reading from
        across the room."""
        if not transcript:
            return ""

        try:
            with open(transcript, "rb") as f:
                f.seek(0, 2)
                f.seek(max(f.tell() - cls.TAIL_BYTES, 0))
                tail = f.read().decode(errors="replace")
        except OSError as e:
            cls.log.debug("transcript unreadable: %s", e)
            return ""

        text = ""
        for line in tail.splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") != "assistant":
                continue
            for block in entry.get("message", {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block["text"]

        # Keeps the opening rather than the tail, and stops on a word: this is
        # the start of what Claude was saying, read at a glance from across the
        # room. Collapsing the layout is the point too - a popup has no room
        # for a heading and a list.
        return textwrap.shorten(text, width=cls.CONTEXT_CHARS, placeholder=" …")

    # ── click-to-focus ────────────────────────────────────────────

    @classmethod
    def focus(cls) -> None:
        """Land the user on this hook's pane, best effort at every step."""
        pane = os.environ.get("TMUX_PANE")
        if not pane or not os.environ.get("TMUX"):
            cls.log.debug("not inside tmux; nothing to focus")
            return

        shown = run(
            [
                "tmux",
                "display-message",
                "-p",
                "-t",
                pane,
                "#{session_name}\t#{window_id}",
            ],
            log=cls.log,
            timeout=cls.RUN_TIMEOUT,
        )
        if shown.returncode != 0:
            return
        session, _, window = shown.stdout.strip().partition("\t")

        run(
            ["tmux", "select-window", "-t", window],
            log=cls.log,
            timeout=cls.RUN_TIMEOUT,
        )
        run(["tmux", "select-pane", "-t", pane], log=cls.log, timeout=cls.RUN_TIMEOUT)
        if client := cls._client(session):
            pid, tty = client
            run(
                ["tmux", "switch-client", "-c", tty, "-t", session],
                log=cls.log,
                timeout=cls.RUN_TIMEOUT,
            )
            cls._focus_kitty(pid)

    @classmethod
    def _client(cls, session: str) -> tuple[int, str] | None:
        """The tmux client to steer: one on our session, else the freshest.

        A detached session has no client of its own, but any attached client
        can be switched to it — that beats a click that goes nowhere."""
        listed = run(
            [
                "tmux",
                "list-clients",
                "-F",
                "#{client_pid}\t#{client_tty}\t#{client_activity}\t#{session_name}",
            ],
            log=cls.log,
            timeout=cls.RUN_TIMEOUT,
        )
        clients = []
        for line in listed.stdout.splitlines():
            pid, tty, activity, name = (line.split("\t") + [""] * 4)[:4]
            if pid.isdigit():
                clients.append((int(pid), tty, int(activity or 0), name))
        if not clients:
            return None

        ours = [c for c in clients if c[3] == session]
        pid, tty, _, _ = max(ours or clients, key=lambda c: c[2])

        return pid, tty

    @classmethod
    def _focus_kitty(cls, client_pid: int) -> None:
        """Raise the kitty window whose shell hosts the tmux client.

        Each kitty instance suffixes the configured socket with its own pid,
        which is also what Hyprland knows the OS window by."""
        ancestors = cls._ancestors(client_pid)
        for sock in glob.glob("/tmp/kitty.sock*"):
            listed = run(
                ["kitty", "@", "--to", f"unix:{sock}", "ls"],
                log=cls.log,
                timeout=cls.RUN_TIMEOUT,
            )
            if listed.returncode != 0:
                continue
            try:
                os_windows = json.loads(listed.stdout)
            except json.JSONDecodeError:
                continue
            for os_window in os_windows:
                for tab in os_window.get("tabs", []):
                    for window in tab.get("windows", []):
                        pids = {
                            p.get("pid") for p in window.get("foreground_processes", [])
                        }
                        pids.add(window.get("pid"))
                        if client_pid not in pids and not pids & ancestors:
                            continue
                        instance = sock.rsplit("-", 1)[-1]
                        if instance.isdigit():
                            cls._focus_hyprland(int(instance))
                        run(
                            [
                                "kitty",
                                "@",
                                "--to",
                                f"unix:{sock}",
                                "focus-window",
                                "--match",
                                f"id:{window['id']}",
                            ],
                            log=cls.log,
                            timeout=cls.RUN_TIMEOUT,
                        )
                        return
        cls.log.debug("no kitty window found for client pid %d", client_pid)

    @classmethod
    def _focus_hyprland(cls, pid: int) -> None:
        """Raise the OS window, wherever its workspace is.

        Hyprland 0.55+ routes `dispatch` through Lua, so the legacy verb form
        is gone; the window is named by address, the way the hypr scripts
        do it."""
        listed = run(["hyprctl", "-j", "clients"], log=cls.log, timeout=cls.RUN_TIMEOUT)
        try:
            clients = json.loads(listed.stdout)
        except json.JSONDecodeError:
            return
        address = next((c.get("address") for c in clients if c.get("pid") == pid), None)
        if address:
            run(
                [
                    "hyprctl",
                    "dispatch",
                    f'hl.dsp.focus({{ window = "address:{address}" }})',
                ],
                log=cls.log,
                timeout=cls.RUN_TIMEOUT,
            )

    @classmethod
    def _ancestors(cls, pid: int) -> set[int]:
        """The process chain above `pid`, for when the tmux client sits under
        a wrapper and is not itself a kitty foreground process."""
        chain: set[int] = set()
        while pid > 1 and pid not in chain:
            chain.add(pid)
            try:
                stat = Path(f"/proc/{pid}/stat").read_text()
            except OSError:
                break
            pid = int(stat.rpartition(")")[2].split()[1])

        return chain


if __name__ == "__main__":
    Notify.cli()
