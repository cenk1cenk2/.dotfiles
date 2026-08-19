#!/usr/bin/env -S sh -c 'exec uv run --project "$(dirname "$0")" "$0" "$@"'

from __future__ import annotations

import json
import logging
import os
import signal
import sys
from pathlib import Path

import click
import psutil

from lib import (
    EnrichAdapter,
    InputAdapter,
    InputMode,
    OutputAdapter,
    OutputMode,
    build_enricher,
    build_input,
    build_output,
    create_logger,
    enrich_options,
    load_prompt,
    notify,
    set_headless,
    signal_waybar,
    spec_from_options,
)

class Copywriter:
    WAYBAR_MODULE = "copywriter"
    ICON = (
        "/usr/share/icons/Adwaita/symbolic/legacy/accessories-text-editor-symbolic.svg"
    )
    SYSTEM_PROMPT = load_prompt("copywriter.md", relative_to=__file__)
    USER_PROMPT = "Clean up the following text:\n<text>\n{text}\n</text>"

    log = logging.getLogger("copywriter")

    def __init__(
        self,
        input: InputAdapter | None = None,
        enricher: EnrichAdapter | None = None,
        output: OutputAdapter | None = None,
    ):
        self._input = input
        self._enricher = enricher
        self._output = output

    # ── core ──────────────────────────────────────────────────────

    def _notify(self, message, timeout=None):
        notify("Copywriter", message, self.ICON, timeout)

    def _find_workers(self) -> list[psutil.Process]:
        """Live `run` workers, excluding self.

        Skips the `uv run` shebang wrapper so `kill` never passes a
        non-session-leader PID to `killpg`. Matches the `run`
        subcommand *positionally* — it's the argument immediately
        after the script path, not the tail, since `run stdout
        --model haiku …` puts the subcommand at index N+1 with
        unrelated options after it."""
        current = os.getpid()
        basename = os.path.basename(__file__)
        workers: list[psutil.Process] = []
        for p in psutil.process_iter(["pid", "cmdline", "name"]):
            if p.info["pid"] == current:
                continue
            if p.info.get("name") == "uv":
                continue
            cmdline = p.info["cmdline"] or []
            script_idx = next(
                (i for i, arg in enumerate(cmdline) if arg and arg.endswith(basename)),
                -1,
            )
            if script_idx < 0:
                continue
            if cmdline[script_idx + 1 : script_idx + 2] != ["run"]:
                continue
            workers.append(p)
        return workers

    def is_running(self) -> bool:
        return bool(self._find_workers())

    def run_once(self) -> None:
        assert self._input is not None, "run requires an input adapter"
        assert self._enricher is not None, "run requires an enrich adapter"
        assert self._output is not None, "run requires an output adapter"

        if self.is_running():
            self.log.info("another copywriter is already running; bailing")
            self._notify("Copywriter is already running")
            return

        # A pipe and a file both have a caller waiting on the payload;
        # forking would return before either holds anything.
        if self._output.mode in (OutputMode.STDOUT, OutputMode.FILE):
            self._execute()
            signal_waybar(self.WAYBAR_MODULE)
            return

        if os.fork() > 0:
            self._notify(
                f"Refining {self._input.mode.value} → {self._output.mode.value}...",
                timeout=2000,
            )
            signal_waybar(self.WAYBAR_MODULE)
            return

        os.setsid()
        try:
            self._execute()
        finally:
            signal_waybar(self.WAYBAR_MODULE)
            os._exit(0)

    def _execute(self) -> None:
        assert (
            self._input is not None
            and self._enricher is not None
            and self._output is not None
        )
        text = self._input.read()
        if not text or not text.strip():
            self.log.warning("%s was empty", self._input.mode.value)
            self._notify(f"{self._input.mode.value.capitalize()} is empty")
            return

        self.log.info("%s text: %d chars", self._input.mode.value, len(text))
        result = self._enricher.enrich(text)
        if not result or not result.strip():
            self.log.warning(
                "enrichment empty; leaving %s unchanged", self._output.mode.value
            )
            self._notify(f"Refinement failed, {self._output.mode.value} unchanged")
            return

        self._output.write(result.strip())
        self.log.info(
            "refined %s → %s (%d chars)",
            self._input.mode.value,
            self._output.mode.value,
            len(result),
        )
        self._notify(
            f"Refined {self._input.mode.value} → {self._output.mode.value}",
            timeout=3000,
        )

    def kill(self) -> None:
        workers = self._find_workers()
        if not workers:
            self._notify("Copywriter is not running")
            return
        for p in workers:
            self.log.info("killing worker pgid=%d", p.pid)
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError) as e:
                self.log.debug("killpg %d: %s", p.pid, e)
        self._notify("Copywriter killed")
        signal_waybar(self.WAYBAR_MODULE)

    def status_json(self) -> str:
        if not self.is_running():
            return json.dumps(
                {"class": "idle", "text": "", "tooltip": "Copywriter ready"}
            )
        return json.dumps(
            {"class": "working", "text": "󰼭 󰧑", "tooltip": "Refining clipboard..."}
        )

    # ── CLI ───────────────────────────────────────────────────────

    @click.group(context_settings={"help_option_names": ["-h", "--help"]})
    @click.option("-v", "--verbose", is_flag=True, help="Enable debug logging.")
    @click.option(
        "--headless", is_flag=True, help="Skip notifications and waybar signals."
    )
    def cli(verbose: bool, headless: bool):
        """Refine clipboard text through AI."""
        create_logger(verbose)
        set_headless(headless)

    @cli.command("run")
    @click.argument(
        "output",
        type=click.Choice([m.value for m in OutputMode], case_sensitive=False),
        default=OutputMode.CLIPBOARD.value,
    )
    @click.option(
        "--input",
        "input_",
        type=click.Choice([m.value for m in InputMode], case_sensitive=False),
        default=InputMode.CLIPBOARD.value,
        help="Text source.",
    )
    @click.option(
        "--input-file", type=click.Path(path_type=Path), help="Text file to read."
    )
    @click.option(
        "--output-file", type=click.Path(path_type=Path), help="Text file to write."
    )
    @enrich_options()
    def cmd_run(
        output,
        output_file,
        input_,
        input_file,
        **enrich_opts,
    ):
        """Refine once and emit to the chosen sink."""
        try:
            input_adapter = build_input(InputMode(input_), path=input_file)
            output_adapter = build_output(OutputMode(output), path=output_file)
        except (TypeError, ValueError) as e:
            raise click.UsageError(str(e)) from e

        spec = spec_from_options(enrich_opts, "copywriter/1.0")
        enricher = build_enricher(
            spec, Copywriter.SYSTEM_PROMPT, Copywriter.USER_PROMPT
        )

        Copywriter(input_adapter, enricher, output_adapter).run_once()

    @cli.command("kill")
    def cmd_kill():
        """Terminate the running worker."""
        Copywriter().kill()

    @cli.command("status")
    def cmd_status():
        """Print waybar-shaped status JSON."""
        sys.stdout.write(Copywriter().status_json() + "\n")

    @cli.command("is-running")
    def cmd_is_running():
        """Exit 0 if a worker is live."""
        sys.exit(0 if Copywriter().is_running() else 1)

if __name__ == "__main__":
    Copywriter.cli()
