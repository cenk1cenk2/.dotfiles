"""Whether a desktop session is there to talk to.

Set once from the CLI group. A headless caller — a pipe, or Hermes
running these scripts on a server — has no notification daemon and no
bar to poke, so the helpers that drive them answer by doing nothing
instead of spawning binaries that are not installed."""

_headless = False


def set_headless(headless: bool) -> None:
    global _headless
    _headless = headless


def is_headless() -> bool:
    return _headless
