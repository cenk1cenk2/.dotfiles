"""Shared CLI + logging scaffolding for the wayland scripts.

Every entry script wires its click root through `create_logger` so
`--verbose` bumps the root to DEBUG and everything else stays INFO.
Rich handler, stderr-bound — stdout is reserved for pipe-friendly
command output (waybar JSON, stdout sinks)."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import IO

from rich.console import Console
from rich.logging import RichHandler

_console: Console | None = None


# Where a run leaves its trace. A compositor keybind has nowhere to put
# stderr, so a script launched from one is undiagnosable without this.
LOG_DIR = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
LOG_CAP = 256 << 10


class CappedFileHandler(logging.FileHandler):
    """Writes until the file reaches `cap`, then stops.

    Keeps the first run rather than the most recent one, which is the
    opposite of what a rotating log does. A fault is read from the time it
    first happened; by the time anyone looks, the runs since have pushed it
    out. Delete the file between runs to arm it again: a run already under
    way keeps writing to the unlinked file it opened."""

    def __init__(self, path: str, cap: int = LOG_CAP):
        super().__init__(path, delay=True)
        self.cap = cap
        try:
            self._written = os.path.getsize(path)
        except OSError:
            self._written = 0

    def emit(self, record: logging.LogRecord) -> None:
        if self._written >= self.cap:
            return
        super().emit(record)
        try:
            self._written = self.stream.tell()
        except (AttributeError, OSError):
            pass


def create_logger(
    verbose: bool,
    *,
    name: str | None = None,
    markup: bool = False,
    log_file: str | None = None,
    quiet: set[str] | frozenset[str] = frozenset(),
) -> logging.Logger:
    """Install a rich handler on the root logger, bound to stderr.

    `markup` opts into rich markup inside log messages, for per-item results
    like `log.info("gpu: [green]%s[/]", name)`. Off by default so a message
    containing square brackets is not silently eaten as a style tag.

    `log_file` names a log under the state directory, which is the only trace
    a run launched from a keybind leaves behind. It follows the console
    level, so `--verbose` is what fills it in.

    `quiet` names subcommands that open no log at all, for the ones a status
    bar polls: left in, they fill the cap before a run worth reading reaches
    it.
    """
    global _console
    root = logging.getLogger()
    level = logging.DEBUG if verbose else logging.INFO
    root.setLevel(level)

    if not any(isinstance(h, RichHandler) for h in root.handlers):
        if _console is None:
            _console = Console(file=sys.stderr, stderr=True, force_terminal=None)
        for h in list(root.handlers):
            root.removeHandler(h)
        handler = RichHandler(
            console=_console,
            show_path=False,
            show_time=True,
            rich_tracebacks=True,
            markup=markup,
            log_time_format="[%H:%M:%S]",
        )
        handler.setLevel(level)
        root.addHandler(handler)
    else:
        for h in root.handlers:
            h.setLevel(level)

    if quiet & set(sys.argv):
        log_file = None

    if log_file and not any(
        isinstance(h, CappedFileHandler) for h in root.handlers
    ):
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            trace = CappedFileHandler(os.path.join(LOG_DIR, log_file))
            trace.setLevel(level)
            trace.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
            )
            root.addHandler(trace)
            # What was actually run. A trace is worth little without it: the
            # flags are the first thing in question when a run behaves like a
            # different command. Straight at the file, because it answers a
            # question nobody watching the console is asking.
            trace.handle(
                logging.LogRecord(
                    "argv", level, __file__, 0, " ".join(sys.argv), (), None
                )
            )
        except OSError as e:
            # A log that cannot be opened is not worth failing a run over.
            root.warning("no trace log: %s", e)

    return logging.getLogger(name) if name else root


@dataclass
class RunResult:
    returncode: int
    stdout: str
    stderr: str


def run(
    cmd: list[str],
    *,
    log: logging.Logger,
    env: dict | None = None,
    cwd: str | None = None,
    input: str | None = None,
    timeout: float | None = None,
    check: bool = False,
    tag: str | None = None,
) -> RunResult:
    """Run `cmd`, streaming stdout+stderr through `log.debug` as lines
    arrive, and return captured output.

    Spawn is logged once at INFO; every subsequent line from either
    stream lands at DEBUG so `--verbose` shows live subprocess chatter.
    Non-zero exits flip to WARNING. `tag` prefixes each streamed line
    (default: basename of argv[0]) so callers running multiple
    subprocesses in parallel can tell them apart in the log."""
    stream_tag = tag or (cmd[0].rsplit("/", 1)[-1] if cmd else "subprocess")
    log.info("spawn: %s", " ".join(cmd))

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=cwd,
        text=True,
        # Own process group so a timeout can take the whole tree down. The AI
        # CLIs fork children of their own — claude runs node, opencode spawns a
        # local server — and killing only the direct child orphans those.
        start_new_session=True,
    )

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []

    def _pump(stream: IO[str], sink: list[str], channel: str) -> None:
        for line in stream:
            sink.append(line)
            log.debug("%s %s: %s", stream_tag, channel, line.rstrip("\n"))

    threads = [
        threading.Thread(
            target=_pump,
            args=(proc.stdout, stdout_chunks, "stdout"),
            daemon=True,
        ),
        threading.Thread(
            target=_pump,
            args=(proc.stderr, stderr_chunks, "stderr"),
            daemon=True,
        ),
    ]
    for t in threads:
        t.start()

    try:
        if input is not None and proc.stdin is not None:
            proc.stdin.write(input)
            proc.stdin.close()
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError, PermissionError:
            proc.kill()
        # Reap, or the killed child lingers as a zombie until this process
        # exits — which for a forked copywriter worker can be a long time.
        proc.wait()
        for t in threads:
            t.join(timeout=1)
        raise
    for t in threads:
        t.join(timeout=1)

    rc = proc.returncode
    if rc != 0:
        log.warning("%s exit=%d", stream_tag, rc)
    result = RunResult(
        returncode=rc,
        stdout="".join(stdout_chunks),
        stderr="".join(stderr_chunks),
    )
    if check and rc != 0:
        raise subprocess.CalledProcessError(rc, cmd, result.stdout, result.stderr)
    return result
