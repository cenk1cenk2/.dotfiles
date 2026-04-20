"""Fire-and-forget background task dispatch.

Callers across the pilot overlay want to run side-work (polling the
agent's session list after a turn, warming MCP caches, nudging a
subprocess) without wiring a fresh `threading.Thread(...)` at every
call site. `BackgroundTaskHandler` centralises the pattern:

  - Threads are daemonised so a stuck task can't block process exit.
  - Each thread carries the caller-supplied `name` so `ps` / `py-spy`
    show which worker is live instead of a sea of `Thread-<n>`.
  - Exceptions land on the module logger via `log.exception` —
    unhandled background exceptions default to stderr in CPython,
    which poisons the pipe-policy for any script that shares the
    terminal.

A module-level singleton (`background_tasks`) is re-exported through
`lib.__init__` so callers reach it with `from lib import
background_tasks`. Headless scripts don't pay any GTK cost for
importing this — it's stdlib-only threading."""

from __future__ import annotations

import logging
import threading
from typing import Callable

log = logging.getLogger(__name__)


class BackgroundTaskHandler:
    """Dispatches named background tasks. Daemon threads, named so
    ps/py-spy show which worker is live. Exceptions land on the logger,
    not stderr, so a failed task doesn't poison the terminal."""

    def submit(self, name: str, target: Callable[[], None]) -> None:
        """Spawn `target` on a named daemon thread.

        Wraps `target` so a raised exception is logged at ERROR with a
        full traceback and swallowed — background tasks are fire-and-
        forget by contract; a thrown exception without this wrapper
        lands on stderr via Python's default thread-excepthook, which
        violates pipe-oriented scripts' stdout/stderr policy."""
        thread = threading.Thread(
            target=self._run, args=(name, target), name=name, daemon=True
        )
        thread.start()

    @staticmethod
    def _run(name: str, target: Callable[[], None]) -> None:
        try:
            target()
        except Exception:
            log.exception("background task %r raised", name)


background_tasks = BackgroundTaskHandler()
