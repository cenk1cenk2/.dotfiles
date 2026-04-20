"""Shared CLI + logging scaffolding for the wayland scripts.

Every entry script wires its click root through `create_logger`
so `--verbose` bumps to DEBUG and everything else stays INFO. All
output lands on stderr — stdout is reserved for pipe-friendly
command output (waybar JSON, stdout sinks)."""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
from typing import IO, Iterable, Optional

from rich.console import Console
from rich.logging import RichHandler

_console: Optional[Console] = None

def create_logger(verbose: bool, *, name: Optional[str] = None) -> logging.Logger:
    """Install a rich stderr handler once per process."""
    root = logging.getLogger()
    level = logging.DEBUG if verbose else logging.INFO
    root.setLevel(level)

    def _stderr_console() -> Console:
        global _console
        if _console is None:
            _console = Console(file=sys.stderr, stderr=True, force_terminal=None)
        return _console

    has_rich = any(isinstance(h, RichHandler) for h in root.handlers)
    if not has_rich:
        for h in list(root.handlers):
            root.removeHandler(h)
        handler = RichHandler(
            console=_stderr_console(),
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

def stream_subprocess_output(
    cmd: Iterable[str],
    *,
    logger: logging.Logger,
    prefix: Optional[str] = None,
    stdin: Optional[bytes] = None,
    extra_env: Optional[dict] = None,
) -> subprocess.CompletedProcess:
    """Run `cmd` and stream stdout/stderr through `logger` at DEBUG.

    Returns a CompletedProcess with captured stdout/stderr when debug
    is off (so callers keep their parsing path). In debug mode every
    line is echoed at DEBUG as it arrives, in addition to being
    buffered for the return value."""
    cmd_list = list(cmd)
    tag = prefix or cmd_list[0]
    logger.debug("spawn: %s", " ".join(cmd_list))

    debug_on = logger.isEnabledFor(logging.DEBUG)
    if not debug_on:
        result = subprocess.run(
            cmd_list,
            input=stdin,
            capture_output=True,
            env=extra_env,
            check=False,
        )
        logger.debug("exit %s: %s", result.returncode, tag)
        return result

    proc = subprocess.Popen(
        cmd_list,
        stdin=subprocess.PIPE if stdin is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=extra_env,
    )

    stdout_buf: list[bytes] = []
    stderr_buf: list[bytes] = []

    def _pump(src: IO[bytes], sink: list[bytes], stream_tag: str) -> None:
        for raw in iter(src.readline, b""):
            sink.append(raw)
            line = raw.rstrip(b"\n").decode("utf-8", errors="replace")
            logger.debug("%s[%s] %s", tag, stream_tag, line)
        src.close()

    threads = []
    if proc.stdout is not None:
        t = threading.Thread(
            target=_pump, args=(proc.stdout, stdout_buf, "out"), daemon=True
        )
        t.start()
        threads.append(t)
    if proc.stderr is not None:
        t = threading.Thread(
            target=_pump, args=(proc.stderr, stderr_buf, "err"), daemon=True
        )
        t.start()
        threads.append(t)

    if stdin is not None and proc.stdin is not None:
        try:
            proc.stdin.write(stdin)
            proc.stdin.close()
        except BrokenPipeError:
            pass

    proc.wait()
    for t in threads:
        t.join()

    logger.debug("exit %s: %s", proc.returncode, tag)
    return subprocess.CompletedProcess(
        args=cmd_list,
        returncode=proc.returncode,
        stdout=b"".join(stdout_buf),
        stderr=b"".join(stderr_buf),
    )
