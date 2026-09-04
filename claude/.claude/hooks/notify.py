#!/usr/bin/env -S sh -c 'exec uv run --project "$(dirname "$0")" "$0" "$@"'

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import click
from dotlib.cli import create_logger
from dotlib.notify import Chime, ChimeDirection, Notification, NotifyChannel, Urgency


class Notify:
    """Claude Code Notification hook: desktop popup with context, plus a chime."""

    # The transcript is JSONL and can run to megabytes; the last assistant
    # message is always inside the final few entries.
    TAIL_BYTES = 64 << 10
    CONTEXT_CHARS = 300
    # Notification messages that must not be missed while glancing away.
    URGENT_WORDS = ("permission", "approval", "waiting for your input")

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
        Notification(
            f"Claude Code{label} — {directory}",
            icon="utilities-terminal",
            channel=NotifyChannel.DESKTOP,
        ).send(body, urgency=urgency)
        Chime(ChimeDirection.UP).play()

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

        return text[: cls.CONTEXT_CHARS]


if __name__ == "__main__":
    Notify.cli()
