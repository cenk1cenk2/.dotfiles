"""Coding agents running in tmux, and the list of those waiting on the user.

The notify hook writes the waiting list; the Stream Deck plugin and the
Hyprland agent menu read and prune it. Every side takes the lock beside it
to rewrite it."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

WAITING = (
    Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
    / "agents-waiting.json"
)
# An entry with no tmux pane can be neither checked nor focused.
PANELESS_SECONDS = 30 * 60
# Process names, as /proc reports them, by the vendor mark the deck draws.
NAMES: dict[str, str] = {
    "claude": "claude",
    "codex": "openai",
    "opencode": "opencode",
}


def edit_waiting(change: Callable[[list[dict]], list[dict]]) -> list[dict]:
    """Rewrite the waiting list under its lock and return what it now holds."""
    with open(WAITING.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            waiting = json.loads(WAITING.read_text())
        except OSError, ValueError:
            waiting = []
        changed = change(waiting)
        if changed != waiting:
            tmp = WAITING.with_suffix(".tmp")
            tmp.write_text(json.dumps(changed))
            tmp.replace(WAITING)

    return changed


def stale(entry: dict, panes: dict[str, dict] | None) -> bool:
    """Whether a waiting entry no longer waits: its agent exited or got back
    to work, or its pane is gone or on screen.

    With no pane listing to check against, only the agent itself is checked,
    and a paneless entry from a hook that found no agent ages out."""
    if (pid := entry.get("pid")) is not None:
        if not alive(pid, entry["name"]):
            return True
        session = entry.get("home") and claude_session(Path(entry["home"]), pid)
        if (
            session
            and session.get("status") == "busy"
            and session.get("statusUpdatedAt", 0) / 1000 > entry.get("at", 0)
        ):
            return True
    if not entry.get("pane"):
        return pid is None and time.time() - entry.get("at", 0) >= PANELESS_SECONDS
    if panes is None:
        return False
    info = panes.get(entry["pane"])

    return info is None or info["seen"]


def tmux_panes() -> dict[str, dict] | None:
    """Every tmux pane by id; None without tmux.

    On screen means the active pane of its session's active window, in a
    session some client is attached to. Kitty's own focus is not asked, which
    would cost a `kitty @ ls` on every poll."""
    cmd = [
        "tmux",
        "list-panes",
        "-a",
        "-F",
        (
            "#{pane_id}\t#{pane_pid}\t#{session_name}:#{window_index}"
            "\t#{pane_current_path}\t#{pane_active}#{window_active}#{session_attached}"
        ),
    ]
    # DEBUG, not INFO: the deck runs this every second.
    log.debug("spawn: %s", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=2, check=False
        )
    except subprocess.TimeoutExpired:
        return None
    if proc.stderr:
        log.debug("tmux stderr: %s", proc.stderr.strip())
    if proc.returncode != 0:
        return None

    panes = {}
    for line in proc.stdout.splitlines():
        pane, pid, where, path, flags = (line.split("\t") + [""] * 5)[:5]
        if pid.isdigit():
            panes[pane] = {
                "pid": int(pid),
                "where": where,
                "path": path,
                "seen": flags[:2] == "11" and flags[2:] not in ("", "0"),
            }

    return panes


def walk(panes: dict[str, dict]) -> dict[str, dict]:
    """The agent in each pane's process tree, by pane.

    Agents run under wrappers (a profile launcher, a shell, node), so the
    pane's own process is rarely the agent and the whole tree is searched.
    That reads all of /proc, which a poller should not do every tick."""
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
            if (vendor := NAMES.get(name)) is not None:
                home = config_dir(current, vendor)
                found[pane] = {
                    "pane": pane,
                    "pid": current,
                    "name": name,
                    "vendor": vendor,
                    "home": home,
                    "profile": home
                    and (home.name.split("-", 1)[1] if "-" in home.name else home.name),
                }
                break
            stack.extend(children.get(current, []))

    return found


def ancestor(pid: int) -> tuple[int, str] | None:
    """The nearest agent process at or above `pid`, as its pid and name.

    A hook runs as a descendant of its agent, which is the only way to name
    the agent when it runs outside tmux, like a parked background session."""
    while pid > 1:
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            return None
        close = stat.rfind(")")
        if (name := stat[stat.find("(") + 1 : close]) in NAMES:
            return pid, name
        pid = int(stat[close + 2 :].split()[1])

    return None


def alive(pid: int, name: str) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False

    return stat[stat.find("(") + 1 : stat.rfind(")")] == name


def config_dir(pid: int, vendor: str) -> Path | None:
    """The state dir an agent runs from, as its profile launcher set it.

    Its name carries the account: `~/.claude-kilic` runs as `kilic`."""
    try:
        environ = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except OSError:
        return None
    variable = {"claude": b"CLAUDE_CONFIG_DIR=", "openai": b"CODEX_HOME="}.get(vendor)
    for item in environ:
        if variable and item.startswith(variable):
            return Path(item[len(variable) :].decode(errors="replace"))

    return None


def claude_session(home: Path, pid: int) -> dict | None:
    """What Claude Code publishes about a running session: its name, status
    (`idle` / `busy`) and session id, under the pid it runs as."""
    try:
        return json.loads((home / "sessions" / f"{pid}.json").read_text())
    except OSError, ValueError:
        return None


def claude_transcript(home: Path, session: str) -> Path | None:
    return next((home / "projects").glob(f"*/{session}.jsonl"), None)


def last_reply(transcript: Path, tail_bytes: int = 64 << 10) -> str:
    """The last assistant text in a Claude Code transcript.

    The transcript is JSONL and can run to megabytes; the last assistant
    message is always inside the final few entries, so only the tail is read."""
    try:
        with transcript.open("rb") as f:
            f.seek(0, 2)
            f.seek(max(f.tell() - tail_bytes, 0))
            tail = f.read().decode(errors="replace")
    except OSError as e:
        log.debug("transcript unreadable: %s", e)
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

    return text
