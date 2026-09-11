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
    """Agent hook: desktop popup with context, plus a chime.

    Serves Claude Code's Notification hook and codex's PermissionRequest /
    Stop hooks. Both hand the same shape on stdin — `cwd` and
    `transcript_path` — and differ only in what names the event: Claude
    sends a ready-made `message`, codex sends `hook_event_name` plus the
    fields that event carries.

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
    @cli.command("claude")
    @click.argument("profile", required=False)
    @click.option("--verbose", "-v", is_flag=True, help="Debug logging.")
    def cmd_claude(profile: str | None, verbose: bool) -> None:
        """Read a Claude Code hook payload from stdin and raise the alarm."""
        create_logger(verbose)

        payload = Notify.payload()
        if payload is None:
            return

        message = payload.get("message") or "Waiting for input"

        Notify.alarm(
            vendor="Claude Code",
            profile=profile,
            payload=payload,
            message=message,
            # The hook's own message is generic ("needs your permission"); what
            # Claude was saying when it stopped is the part worth reading from
            # across the room.
            context=Notify.transcript(payload.get("transcript_path")),
        )

    @staticmethod
    @cli.command("codex")
    @click.argument("profile", required=False)
    @click.option("--verbose", "-v", is_flag=True, help="Debug logging.")
    def cmd_codex(profile: str | None, verbose: bool) -> None:
        """Read a codex hook payload from stdin and raise the alarm."""
        create_logger(verbose)

        payload = Notify.payload()
        if payload is None:
            return

        # Codex names the event rather than writing the prose, and carries the
        # answer inline on Stop — so no transcript parsing on either path, and
        # `URGENT_WORDS` still decides urgency off the sentence built here.
        match payload.get("hook_event_name"):
            case "PermissionRequest":
                tool = payload.get("tool_name") or "a tool"
                message = f"Needs approval to run {tool}"
                context = ""
            case "Stop":
                message = "Turn finished"
                context = Notify.shorten(payload.get("last_assistant_message"))
            case other:
                message = f"{other or 'Codex'} fired"
                context = ""

        Notify.alarm(
            vendor="Codex",
            profile=profile,
            payload=payload,
            message=message,
            context=context,
        )

    @staticmethod
    @cli.command("focus")
    @click.option("--verbose", "-v", is_flag=True, help="Debug logging.")
    def cmd_focus(verbose: bool) -> None:
        """Jump to this pane without a popup, for testing the chain."""
        create_logger(verbose)
        Notify.focus()

    @classmethod
    def payload(cls) -> dict | None:
        """The hook payload, or None when the user is already watching.

        The focus check comes before the read so a watched pane costs no
        parsing, and an unreadable payload still alarms — the event happened
        whether or not its JSON survived."""
        if cls.focused():
            cls.log.debug("pane already in focus; staying quiet")
            return None

        try:
            return json.load(sys.stdin)
        except json.JSONDecodeError as e:
            cls.log.warning("bad hook payload: %s", e)
            return {}

    @classmethod
    def alarm(
        cls,
        vendor: str,
        profile: str | None,
        payload: dict,
        message: str,
        context: str,
    ) -> None:
        """Chime, mark the terminal, and pop a popup that focuses on click."""
        directory = Path(payload.get("cwd") or "/").name or "/"

        urgency = Urgency.NORMAL
        if any(word in message.lower() for word in cls.URGENT_WORDS):
            urgency = Urgency.CRITICAL

        body = f"{message}\n\n{context}" if context else message

        label = f" ({profile})" if profile else ""
        # Before the popup rather than after: the popup call blocks for its
        # lifetime waiting on a click, and the sound belongs to its appearance
        # rather than its dismissal.
        Chime(ChimeDirection.UP).play()
        # Marks the tmux window and the kitty tab, which the popup cannot do
        # for a session the user is not currently looking at.
        bell()
        clicked = Notification(
            f"{vendor}{label} — {directory}",
            icon="utilities-terminal",
            channel=NotifyChannel.DESKTOP,
        ).send(
            body,
            timeout=cls.SHOW_MS,
            urgency=urgency,
            actions=[("default", "Focus")],
        )
        if clicked:
            cls.focus()

    @classmethod
    def transcript(cls, transcript: str | None) -> str:
        """The last assistant text in a Claude Code transcript.

        Claude-only: codex hands the same text inline on Stop, so nothing
        reads its transcript and its JSONL shape stays unparsed here."""
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

        return cls.shorten(text)

    @classmethod
    def shorten(cls, text: str | None) -> str:
        """Keeps the opening rather than the tail, and stops on a word: this is
        the start of what the agent was saying, read at a glance from across
        the room. Collapsing the layout is the point too - a popup has no room
        for a heading and a list."""
        return textwrap.shorten(text or "", width=cls.CONTEXT_CHARS, placeholder=" …")

    # ── the pane this hook runs in ────────────────────────────────

    @classmethod
    def focused(cls) -> bool:
        """Whether the user is already watching the pane this hook fired in.

        Every layer has to agree: the pane is the active one in its tmux
        window, that window is the session's current one, and kitty has focus
        on the window hosting a client attached to it. Anything unreadable
        answers no — an alarm nobody needed beats one nobody got."""
        pane = os.environ.get("TMUX_PANE")
        if not pane or not os.environ.get("TMUX"):
            return cls._kitty_focused(os.getpid())

        shown = run(
            [
                "tmux",
                "display-message",
                "-p",
                "-t",
                pane,
                "#{pane_active}\t#{window_active}\t#{session_name}",
            ],
            log=cls.log,
            timeout=cls.RUN_TIMEOUT,
        )
        if shown.returncode != 0:
            return False
        active_pane, active_window, session = (
            shown.stdout.strip().split("\t") + [""] * 3
        )[:3]
        if active_pane != "1" or active_window != "1":
            return False

        # Scoped to the session rather than every client: another terminal
        # attached elsewhere has focus of its own and says nothing about this
        # pane.
        listed = run(
            ["tmux", "list-clients", "-t", session, "-F", "#{client_pid}"],
            log=cls.log,
            timeout=cls.RUN_TIMEOUT,
        )

        return any(
            pid.isdigit() and cls._kitty_focused(int(pid))
            for pid in listed.stdout.split()
        )

    @classmethod
    def _kitty_focused(cls, pid: int) -> bool:
        """Whether the kitty window running `pid` is the one being looked at.

        All three flags, not the window's own: kitty marks the active window
        of every tab, so `is_active` alone is true for a pane sitting behind
        another tab or another OS window."""
        found = cls._locate(pid)
        if not found:
            return False

        _, os_window, tab, window = found

        return bool(
            os_window.get("is_focused")
            and tab.get("is_active")
            and window.get("is_active")
        )

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
        """Raise the kitty window whose shell hosts the tmux client."""
        found = cls._locate(client_pid)
        if not found:
            cls.log.debug("no kitty window found for client pid %d", client_pid)
            return

        sock, _, _, window = found
        # Each kitty instance suffixes the configured socket with its own pid,
        # which is also what Hyprland knows the OS window by.
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

    @classmethod
    def _locate(cls, pid: int) -> tuple[str, dict, dict, dict] | None:
        """The kitty socket, OS window, tab and window running `pid`.

        A tmux client sits under a wrapper rather than being a foreground
        process itself, so the chain above it counts as a match too."""
        ancestors = cls._ancestors(pid)
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
                        if pid in pids or pids & ancestors:
                            return sock, os_window, tab, window

        return None

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
