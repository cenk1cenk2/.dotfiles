#!/usr/bin/env -S sh -c 'd="$(dirname "$0")"; uv sync -q --project "$d" && exec "$d/.venv/bin/python" "$0" "$@"'

from __future__ import annotations

import glob
import json
import logging
import os
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import ClassVar

import click
from dotlib.agents import (
    DEFAULT_SERVER,
    NAMES,
    ancestor,
    config_dir,
    edit_waiting,
    last_reply,
    own_pane,
    split_ref,
    tmux,
)
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

    Serves Claude Code's Notification hook, codex's PermissionRequest /
    Stop hooks and the opencode and Amp notify plugins, which send codex's
    shape. All hand JSON on stdin carrying `cwd` and `session_id` and differ
    in what names the event: Claude sends a ready-made `message` and a
    `transcript_path`, codex, opencode and Amp send `hook_event_name` plus the
    fields that event carries (`tool_name`, `last_assistant_message`).
    opencode adds a `Question` event for a form awaiting an answer, its title
    in `tool_name`; Amp adds the thread's `url`.

    Clicking the popup focuses the pane this hook was spawned in: the right
    tmux window on the right client, and the kitty window hosting it. Outside
    tmux, an Amp thread opens at its URL instead."""

    CONTEXT_CHARS = 300
    # Notification messages that must not be missed while glancing away.
    URGENT_WORDS = ("permission", "approval", "waiting for your input")
    # Subcommands taking codex's payload, and the vendor each one names.
    CODEX_SHAPED: ClassVar[dict[str, str]] = {
        "codex": "Codex",
        "opencode": "OpenCode",
        "amp": "Amp",
    }
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
    @click.pass_context
    def cmd_codex(ctx: click.Context, profile: str | None, verbose: bool) -> None:
        """Read a codex-shaped hook payload from stdin and raise the alarm."""
        create_logger(verbose)

        payload = Notify.payload()
        if payload is None:
            return

        vendor = Notify.CODEX_SHAPED[ctx.info_name]
        # The event is named rather than written as prose, and the answer rides
        # inline on Stop — so no transcript parsing on either path, and
        # `URGENT_WORDS` still decides urgency off the sentence built here.
        match payload.get("hook_event_name"):
            case "PermissionRequest":
                tool = payload.get("tool_name") or "a tool"
                message = f"Needs approval to run {tool}"
                context = ""
            case "Question":
                # opencode only: a form awaiting an answer, titled by its question.
                message = "Waiting for your input"
                context = Notify.shorten(payload.get("tool_name"))
            case "Stop":
                message = "Turn finished"
                context = Notify.shorten(payload.get("last_assistant_message"))
            case other:
                message = f"{other or vendor} fired"
                context = ""

        Notify.alarm(
            vendor=vendor,
            profile=profile,
            payload=payload,
            message=message,
            context=context,
        )

    # opencode and Amp have no hooks; their notify plugins speak codex's shape,
    # so every name runs one command and differs only in the vendor label.
    cli.add_command(cli.commands["codex"], "opencode")
    cli.add_command(cli.commands["codex"], "amp")

    @staticmethod
    @cli.command("focus")
    @click.option("--pane", help="Tmux pane to focus; defaults to this one.")
    @click.option("--url", help="Thread URL to open instead of a pane.")
    @click.option("--verbose", "-v", is_flag=True, help="Debug logging.")
    def cmd_focus(pane: str | None, url: str | None, verbose: bool) -> None:
        """Jump to a pane, or open a thread, without a popup."""
        create_logger(verbose)
        if url and not pane:
            Notify.open(url)
        else:
            Notify.focus(pane)

    @staticmethod
    @cli.command("dismiss")
    @click.option("--session", required=True, help="Session whose entry to drop.")
    @click.option("--verbose", "-v", is_flag=True, help="Debug logging.")
    def cmd_dismiss(session: str, verbose: bool) -> None:
        """Drop a session from the waiting list once its user is back.

        A pane's entry clears itself when the pane comes on screen; an entry
        with no pane, such as a runner's Amp thread, has nothing to watch and
        is dropped by the plugin when the next prompt starts."""
        create_logger(verbose)
        edit_waiting(
            lambda waiting: [w for w in waiting if w.get("session") != session]
        )

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

        url = payload.get("url")
        cls.record(
            vendor,
            profile,
            payload.get("session_id"),
            directory,
            message,
            context,
            urgency == Urgency.CRITICAL,
            url,
        )

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
            if url and not os.environ.get("TMUX"):
                cls.open(url)
            else:
                cls.focus()

    @classmethod
    def record(
        cls,
        vendor: str,
        profile: str | None,
        session: str | None,
        directory: str,
        message: str,
        context: str,
        urgent: bool,
        url: str | None,
    ) -> None:
        """List this agent as waiting, replacing its previous entry by pane or,
        outside tmux, by session."""
        pane = own_pane()
        found = ancestor(os.getppid())
        home = found and config_dir(found[0], NAMES[found[1]])
        entry = {
            "pane": pane,
            "session": session,
            "pid": found and found[0],
            "name": found and found[1],
            "home": home and str(home),
            "vendor": vendor,
            "profile": profile,
            "directory": directory,
            "message": message,
            "context": context,
            "urgent": urgent,
            "url": url,
            "at": time.time(),
        }
        edit_waiting(
            lambda waiting: [
                *(
                    w
                    for w in waiting
                    if not (pane and w.get("pane") == pane)
                    and not (pane is None and session and w.get("session") == session)
                ),
                entry,
            ]
        )

    @classmethod
    def transcript(cls, transcript: str | None) -> str:
        """Claude-only: codex hands the same text inline on Stop, so nothing
        reads its transcript and its JSONL shape stays unparsed here."""
        return cls.shorten(last_reply(Path(transcript))) if transcript else ""

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
    def open(cls, url: str) -> None:
        """Open a thread in the Amp desktop app, or the browser without one.

        Detached rather than through `run`: a desktop app this starts lives
        on, and a timeout would kill it. A running app takes the URL into its
        own instance and this one exits at once. Hyprland does not let a
        launched app take focus by itself, so the Amp window is focused here."""
        if desktop := shutil.which("amp-desktop"):
            run(
                [
                    "hyprctl",
                    "dispatch",
                    'hl.dsp.focus({ window = "class:amp-desktop" })',
                ],
                log=cls.log,
                timeout=cls.RUN_TIMEOUT,
            )
        cmd = [desktop or "xdg-open", url]
        cls.log.debug("spawn: %s", " ".join(cmd))
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    @classmethod
    def focus(cls, ref: str | None = None) -> None:
        """Land the user on a pane, this hook's own by default, best effort at
        every step.

        `ref` names the pane across tmux servers (`dotlib.agents.pane_ref`).
        A pane on the popup plugin's server is shown by opening its popup on
        the default server's client, since a closed popup has no client."""
        ref = ref or own_pane()
        if not ref:
            cls.log.debug("not inside tmux; nothing to focus")
            return
        pane, server = split_ref(ref)
        on = tmux(server)

        shown = run(
            [*on, "display-message", "-p", "-t", pane, "#{session_name}\t#{window_id}"],
            log=cls.log,
            timeout=cls.RUN_TIMEOUT,
        )
        if shown.returncode != 0:
            return
        session, _, window = shown.stdout.strip().partition("\t")

        run([*on, "select-window", "-t", window], log=cls.log, timeout=cls.RUN_TIMEOUT)
        run([*on, "select-pane", "-t", pane], log=cls.log, timeout=cls.RUN_TIMEOUT)
        popup = server != DEFAULT_SERVER and server == cls._popup_server()
        client = cls._client(on, session, any_session=not popup)
        if client:
            run(
                [*on, "switch-client", "-c", client[1], "-t", session],
                log=cls.log,
                timeout=cls.RUN_TIMEOUT,
            )
        elif popup:
            cls._open_popup(session)
        else:
            cls.log.debug("no client on tmux server %s; nothing to show", server)
            return
        # A popup's client runs under the default server, not under kitty, so
        # kitty is found through the default server's client.
        if popup:
            client = cls._client(tmux(DEFAULT_SERVER), None)
        if client:
            cls._focus_kitty(client[0])

    @classmethod
    def _popup_server(cls) -> str | None:
        """The server tmux-toggle-popup keeps its popup sessions on."""
        shown = run(
            [*tmux(DEFAULT_SERVER), "show-options", "-gv", "@popup-socket-name"],
            log=cls.log,
            timeout=cls.RUN_TIMEOUT,
        )
        return shown.stdout.strip() or None if shown.returncode == 0 else None

    @classmethod
    def _open_popup(cls, session: str) -> None:
        """Open a popup session through the plugin's own toggle, from the
        default server's freshest client, sized as its key bindings size it."""
        if (client := cls._client(tmux(DEFAULT_SERVER), None)) is None:
            cls.log.debug("no default tmux client to open popup %s in", session)
            return
        current = run(
            [
                *tmux(DEFAULT_SERVER),
                "display-message",
                "-p",
                "-c",
                client[1],
                "#{pane_id}",
            ],
            log=cls.log,
            timeout=cls.RUN_TIMEOUT,
        )
        if current.returncode != 0:
            return
        run(
            [
                *tmux(DEFAULT_SERVER),
                "run-shell",
                "-b",
                "-t",
                current.stdout.strip(),
                (
                    f"#{{@popup-toggle}} --id '{session}'"
                    " -x#{E:@popup-width} -y#{E:@popup-height}"
                    " -X#{E:@popup-x} -Y#{E:@popup-y}"
                ),
            ],
            log=cls.log,
            timeout=cls.RUN_TIMEOUT,
        )

    @classmethod
    def _client(
        cls, on: list[str], session: str | None, any_session: bool = True
    ) -> tuple[int, str] | None:
        """The tmux client to steer: one on `session`, else the freshest, unless
        `any_session` is off.

        A detached session has no client of its own, but any attached client
        can be switched to it — that beats a click that goes nowhere."""
        listed = run(
            [
                *on,
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
        if not ours and not any_session:
            return None
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
