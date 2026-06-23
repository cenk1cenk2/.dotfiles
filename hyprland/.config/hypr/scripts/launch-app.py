#!/usr/bin/env -S sh -c 'exec uv run --project "$(dirname "$0")" "$0" "$@"'
"""Launch an app command from Hyprland Lua definitions."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
import click

from lib import Hyprctl, create_logger


class LaunchApp:
    log = logging.getLogger("launch-app")

    def __init__(self, definitions: Path, hypr: Hyprctl):
        self._definitions = definitions
        self._hypr = hypr

    def command_for(self, name: str) -> str:
        lua = (
            "local definitions = dofile(os.getenv('HYPR_DEFINITIONS')); "
            "local app = definitions.apps[os.getenv('HYPR_APP')]; "
            "if app == nil then os.exit(2) end; "
            "io.write(app)"
        )
        cmd = ["lua", "-e", lua]
        env = os.environ.copy()
        env["HYPR_DEFINITIONS"] = str(self._definitions)
        env["HYPR_APP"] = name
        self.log.debug("spawn: %s", " ".join(cmd))
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)
        if proc.stderr:
            self.log.debug("lua stderr: %s", proc.stderr.strip())
        if proc.returncode == 2:
            raise click.ClickException(f"unknown app: {name}")
        if proc.returncode != 0:
            raise click.ClickException("failed to read Hyprland definitions.lua")
        command = proc.stdout.strip()
        if not command:
            raise click.ClickException(f"empty app command: {name}")

        return command

    def launch(self, name: str, *, print_only: bool = False) -> None:
        command = self.command_for(name)
        if print_only:
            click.echo(command)
            return

        expression = f"hl.dsp.exec_cmd({json.dumps(command)})"
        self.log.debug("hypr dispatch: %s", expression)
        if not self._hypr.dispatch(expression):
            raise click.ClickException(f"failed to launch app: {name}")


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("name")
@click.option("--definitions", type=click.Path(path_type=Path), help="Path to definitions.lua.")
@click.option("--print", "print_only", is_flag=True, help="Print the resolved command.")
@click.option("-v", "--verbose", is_flag=True, help="Show subprocess traces.")
def cmd_main(name: str, definitions: Path | None, print_only: bool, verbose: bool) -> None:
    create_logger(verbose, name="launch-app")
    script_dir = Path(__file__).resolve().parent
    LaunchApp(
        definitions=definitions or script_dir.parent / "definitions.lua",
        hypr=Hyprctl(),
    ).launch(name, print_only=print_only)


LaunchApp.cli = cmd_main


if __name__ == "__main__":
    LaunchApp.cli()
