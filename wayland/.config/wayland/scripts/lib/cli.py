"""Shared CLI + logging scaffolding for the wayland scripts.

Every entry script wires its click root through `create_logger` so
`--verbose` bumps the root to DEBUG and everything else stays INFO.
Rich handler, stderr-bound — stdout is reserved for pipe-friendly
command output (waybar JSON, stdout sinks)."""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import IO, Optional

from rich.console import Console
from rich.logging import RichHandler

_console: Optional[Console] = None


def create_logger(verbose: bool, *, name: Optional[str] = None) -> logging.Logger:
    """Install a rich handler on the root logger, bound to stderr."""
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
            markup=False,
            log_time_format="[%H:%M:%S]",
        )
        handler.setLevel(level)
        root.addHandler(handler)
    else:
        for h in root.handlers:
            h.setLevel(level)

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
    env: Optional[dict] = None,
    cwd: Optional[str] = None,
    input: Optional[str] = None,
    timeout: Optional[float] = None,
    check: bool = False,
    tag: Optional[str] = None,
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
        proc.kill()
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
