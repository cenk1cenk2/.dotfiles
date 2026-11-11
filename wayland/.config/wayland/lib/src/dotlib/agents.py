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
# The tmux server `tmux` reaches without -L, whose panes keep their bare id.
DEFAULT_SERVER = "default"
# An entry with no tmux pane can be neither checked nor focused.
PANELESS_SECONDS = 30 * 60
# Process names, as /proc reports them, by the vendor mark the deck draws.
NAMES: dict[str, str] = {
    "claude": "claude",
    "codex": "openai",
    "opencode": "opencode",
    "amp.exe": "amp",
}
# Desktop apps whose windows are agents in their own right, by window class,
# to the vendor mark the deck draws.
WINDOWS: dict[str, str] = {
    "amp-desktop": "amp",
}
# The threads Amp's desktop app runs on this machine's runners, one file each,
# kept by the amp notify plugin (~/.config/amp/plugins/notify.ts).
THREADS = WAITING.parent / "agents" / "amp"
# Amp has no event for a thread closing, so an idle thread drops off after this.
THREAD_IDLE_SECONDS = 12 * 60 * 60


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


def pane_ref(pane: str, server: str) -> str:
    """A pane's name across tmux servers, which all number panes from %0: the
    bare id on the default server, `%id@server` on any other (the popup
    plugin's, Amp's terminals)."""
    return pane if server == DEFAULT_SERVER else f"{pane}@{server}"


def split_ref(ref: str) -> tuple[str, str]:
    """A pane name's tmux pane id and server."""
    pane, _, server = ref.partition("@")
    return pane, server or DEFAULT_SERVER


def tmux(server: str) -> list[str]:
    return ["tmux", "-L", server]


def servers() -> list[str]:
    """The tmux servers with a socket in this user's tmux directory."""
    root = Path(os.environ.get("TMUX_TMPDIR") or "/tmp") / f"tmux-{os.getuid()}"
    try:
        return sorted(path.name for path in root.iterdir() if path.is_socket())
    except OSError:
        return []


def own_pane() -> str | None:
    """The pane this process runs in, named across servers; None outside tmux."""
    pane, socket = os.environ.get("TMUX_PANE"), os.environ.get("TMUX")
    if not pane or not socket:
        return None

    return pane_ref(pane, Path(socket.split(",")[0]).name)


def tmux_panes() -> dict[str, dict] | None:
    """Every tmux pane on every server, by `pane_ref`; None without tmux.

    On screen means the active pane of its session's active window, in a
    session some client is attached to. Kitty's own focus is not asked, which
    would cost a `kitty @ ls` on every poll."""
    panes: dict[str, dict] | None = None
    for server in servers():
        cmd = [
            *tmux(server),
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
            continue
        if proc.stderr:
            log.debug("tmux stderr: %s", proc.stderr.strip())
        # A socket left by a server that has exited answers with an error.
        if proc.returncode != 0:
            continue

        panes = panes or {}
        for line in proc.stdout.splitlines():
            pane, pid, where, path, flags = (line.split("\t") + [""] * 5)[:5]
            if pid.isdigit():
                panes[pane_ref(pane, server)] = {
                    "pid": int(pid),
                    "where": where if server == DEFAULT_SERVER else f"{server} {where}",
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
            if (vendor := NAMES.get(name)) is not None and not headless(current):
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


def running(panes: dict[str, dict], found: dict[str, dict]) -> list[dict]:
    """The agents `walk` found, in tmux's pane order, with where each sits."""
    return [
        {
            **agent,
            "where": panes[pane]["where"],
            "project": Path(panes[pane]["path"]).name or "/",
            "seen": panes[pane]["seen"],
        }
        for pane, agent in found.items()
        if pane in panes
    ]


def windows(clients: list[dict], focused: str | None) -> list[dict]:
    """Agent desktop apps' own windows, shaped as `running` shapes tmux agents:
    a Hyprland window address where a pane would be, on screen while focused.

    Takes Hyprland's `clients` rather than asking for them, so each caller
    keeps its own IPC and decides how often to read it.

    One Amp window shows many threads, so it lists as one entry per thread
    its runners report, each carrying the thread's `url` and `state`."""
    found = []
    for client in clients:
        if (vendor := WINDOWS.get(client.get("class", ""))) is None:
            continue
        # Titles read "<page> - <App>".
        title = client.get("title", "")
        window = {
            "pane": None,
            "window": client["address"],
            "pid": client.get("pid"),
            "name": client["class"],
            "vendor": vendor,
            "home": None,
            "profile": None,
            "where": f"workspace {client.get('workspace', {}).get('name', '?')}",
            "project": title.rpartition(" - ")[0] or title or vendor,
            "url": None,
            "state": None,
            "cwd": None,
        }
        listed = threads() if vendor == "amp" else []
        for entry in listed or [{}]:
            if entry:
                entry = {
                    **window,
                    "profile": entry.get("profile"),
                    "project": entry.get("title") or "new thread",
                    "url": entry.get("url"),
                    "state": entry.get("state"),
                    "cwd": entry.get("cwd"),
                }
            else:
                entry = window
            found.append({**entry, "seen": on_screen(entry, focused, title)})

    return found


def on_screen(entry: dict, address: str | None, title: str) -> bool:
    """Whether an app entry is what the focused window shows: its window, and
    for a thread the page the window's title names."""
    if entry["window"] != address:
        return False

    return entry.get("url") is None or title.rpartition(" - ")[0] == entry["project"]


def threads() -> list[dict]:
    """Amp desktop threads live on this machine's runners, newest first.

    A runner that has exited takes its threads with it, its files removed."""
    found = []
    now = time.time()
    for path in THREADS.glob("*.json"):
        try:
            thread = json.loads(path.read_text())
        except OSError, json.JSONDecodeError:
            continue
        runner = thread.get("runner")
        try:
            os.kill(runner, 0)
        except ProcessLookupError, TypeError:
            path.unlink(missing_ok=True)
            continue
        except PermissionError:
            pass
        idle = thread.get("state") == "idle"
        if idle and now - thread.get("updated", 0) > THREAD_IDLE_SECONDS:
            continue
        found.append(thread)

    return sorted(found, key=lambda t: -t.get("updated", 0))


def joined(panes: list[dict], windows: list[dict]) -> list[dict]:
    """Tmux agents then app windows, as one list to step through.

    A pane counts as on screen while it is its attached session's active one,
    which says nothing about the compositor: with an app window focused, that
    window is what is on screen and no pane is."""
    if any(window["seen"] for window in windows):
        panes = [{**pane, "seen": False} for pane in panes]

    return panes + windows


def step(agents: list[dict], offset: int) -> dict | None:
    """The agent `offset` places from the one on screen, wrapping; from the
    start of the list when none is on screen."""
    if not agents:
        return None
    current = next((i for i, a in enumerate(agents) if a["seen"]), -1)

    return agents[(current + offset) % len(agents)]


def waiting(panes: dict[str, dict] | None) -> list[dict]:
    """The waiting list with stale entries pruned, oldest first."""
    return sorted(
        edit_waiting(lambda w: [e for e in w if not stale(e, panes)]),
        key=lambda e: e.get("at", 0),
    )


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


def headless(pid: int) -> bool:
    """Whether an agent runs with no prompt to type into, as Amp's desktop
    server does under the same process name as its terminal client."""
    try:
        return b"--no-tui" in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return True


def config_dir(pid: int, vendor: str) -> Path | None:
    """The state dir an agent runs from, as its profile launcher set it.

    Its name carries the account: `~/.claude-kilic` runs as `kilic`."""
    try:
        environ = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except OSError:
        return None
    variable = {
        "claude": b"CLAUDE_CONFIG_DIR=",
        "openai": b"CODEX_HOME=",
        "amp": b"AMP_SETTINGS_FILE=",
    }.get(vendor)
    for item in environ:
        if variable and item.startswith(variable):
            path = Path(item[len(variable) :].decode(errors="replace"))
            # Amp names its settings file, `~/.config/amp-kilic/settings.json`.
            return path.parent if vendor == "amp" else path

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
