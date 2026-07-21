#!/usr/bin/env -S sh -c 'exec uv run --project "$(dirname "$0")" "$0" "$@"'
"""Launch an app command from Hyprland Lua definitions."""

from __future__ import annotations

import json
import logging
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
        proc = self._hypr.run_lua(lua, {"HYPR_DEFINITIONS": str(self._definitions), "HYPR_APP": name})
        if proc.returncode == 2:
            raise click.ClickException(f"unknown app: {name}")
        if proc.returncode != 0:
            detail = proc.stderr.strip().splitlines()[0] if proc.stderr.strip() else f"exit {proc.returncode}"
            raise click.ClickException(f"failed to read Hyprland definitions.lua: {detail}")
        command = proc.stdout.strip()
        if not command:
            raise click.ClickException(f"empty app command: {name}")

        return command

    def names(self) -> list[str]:
        lua = (
            "local definitions = dofile(os.getenv('HYPR_DEFINITIONS')); "
            "local names = {}; "
            "for name in pairs(definitions.apps) do names[#names + 1] = name end; "
            "table.sort(names); "
            "io.write(table.concat(names, '\\n'))"
        )
        proc = self._hypr.run_lua(lua, {"HYPR_DEFINITIONS": str(self._definitions)})
        if proc.returncode != 0:
            detail = proc.stderr.strip().splitlines()[0] if proc.stderr.strip() else f"exit {proc.returncode}"
            raise click.ClickException(f"failed to read Hyprland definitions.lua: {detail}")

        return proc.stdout.split()

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
@click.argument("name", required=False)
@click.option("--definitions", type=click.Path(path_type=Path), help="Path to definitions.lua.")
@click.option("--print", "print_only", is_flag=True, help="Print the resolved command.")
@click.option("--list", "list_apps", is_flag=True, help="List app names.")
@click.option("-v", "--verbose", is_flag=True, help="Show subprocess traces.")
def cmd_main(
    name: str | None, definitions: Path | None, print_only: bool, list_apps: bool, verbose: bool
) -> None:
    create_logger(verbose, name="launch-app")
    script_dir = Path(__file__).resolve().parent
    app = LaunchApp(
        definitions=definitions or script_dir.parent / "definitions.lua",
        hypr=Hyprctl(),
    )
    if list_apps:
        for n in app.names():
            click.echo(n)
        return
    if not name:
        raise click.UsageError("NAME is required unless --list is given.")
    app.launch(name, print_only=print_only)


LaunchApp.cli = cmd_main


if __name__ == "__main__":
    LaunchApp.cli()
