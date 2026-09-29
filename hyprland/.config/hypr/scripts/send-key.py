#!/usr/bin/env -S sh -c 'exec uv run --project "$(dirname "$0")" "$0" "$@"'
"""Send a shortcut to a window without focusing it."""

from __future__ import annotations

import json
import logging
import time

import click

from dotlib.cli import create_logger
from lib import Hyprctl

class SendKey:
    log = logging.getLogger("send-key")
    SETTLE = 0.15

    def __init__(self, hypr: Hyprctl):
        self._hypr = hypr

    def dispatch(self, expression: str) -> bool:
        self.log.debug("hypr dispatch: %s", expression)

        return self._hypr.dispatch(expression)

    def focus(self, window: str) -> bool:
        return self.dispatch(f"hl.dsp.focus({{ window = {json.dumps(window)} }})")

    def send(self, window: str, key: str, mods: str, *, focus: bool = False) -> None:
        """WINDOW is a Hyprland window selector (`class:com.cuperino.qprompt`),
        KEY an xkb key name (`F9`, `space`, `Page_Up`).

        Without focus the key reaches whichever widget last had focus inside
        the window, which in some apps is a text field that swallows it.
        `focus` delivers it as a typed key, then hands focus back. Qt only
        routes keys once it has processed the activation, so both focus
        switches wait `SETTLE` or the key lands on an inactive window."""
        previous = self._hypr.active_window() if focus else None
        if focus:
            if not self.focus(window):
                raise click.ClickException(f"failed to focus {window}")
            time.sleep(self.SETTLE)
        expression = (
            f"hl.dsp.send_shortcut({{ mods = {json.dumps(mods)}, "
            f"key = {json.dumps(key)}, window = {json.dumps(window)} }})"
        )
        sent = self.dispatch(expression)
        if previous:
            time.sleep(self.SETTLE)
            self.focus(f"address:{previous['address']}")
        if not sent:
            raise click.ClickException(f"failed to send {mods}+{key} to {window}")

@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("window")
@click.argument("key")
@click.option("-m", "--mods", default="", help="Modifiers, e.g. CTRL or CTRL+SHIFT.")
@click.option("-f", "--focus", is_flag=True, help="Focus the window for the key.")
@click.option("-v", "--verbose", is_flag=True, help="Show dispatch traces.")
def cmd_main(window: str, key: str, mods: str, focus: bool, verbose: bool) -> None:
    create_logger(verbose, name="send-key")
    SendKey(Hyprctl()).send(window, key, mods, focus=focus)

SendKey.cli = cmd_main

if __name__ == "__main__":
    SendKey.cli()
