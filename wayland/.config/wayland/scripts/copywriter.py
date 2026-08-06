#!/usr/bin/env -S sh -c 'exec uv run --project "$(dirname "$0")" "$0" "$@"'

from __future__ import annotations

import json
import logging
import os
import signal
import sys
from typing import Optional

import click
import psutil

from lib import (
    DEFAULT_API_KEY_ENV,
    DEFAULT_BASE_URL,
    DEFAULT_ENRICH_ADAPTER,
    DEFAULT_ENRICH_MODE,
    DEFAULT_TIMEOUT,
    EnrichAdapter,
    EnrichMode,
    EnrichProvider,
    EnrichSpec,
    InputAdapter,
    InputAdapterClipboard,
    InputAdapterStdin,
    InputMode,
    OutputAdapter,
    OutputAdapterClipboard,
    OutputAdapterStdout,
    OutputAdapterType,
    OutputMode,
    build_enricher,
    create_logger,
    load_prompt,
    notify,
    signal_waybar,
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
        input: Optional[InputAdapter] = None,
        enricher: Optional[EnrichAdapter] = None,
        output: Optional[OutputAdapter] = None,
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

        if self._output.mode is OutputMode.STDOUT:
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
    def cli(verbose: bool):
        """Refine clipboard text through AI."""
        create_logger(verbose)

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
        "--provider",
        type=click.Choice([p.value for p in EnrichProvider], case_sensitive=False),
        default=DEFAULT_ENRICH_ADAPTER.value,
        help="Enrichment backend.",
    )
    @click.option(
        "--mode",
        type=click.Choice([m.value for m in EnrichMode], case_sensitive=False),
        default=DEFAULT_ENRICH_MODE.value,
        help="Capability ceiling.",
    )
    @click.option(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help="HTTP backend base URL.",
    )
    @click.option(
        "--api-key-env",
        default=DEFAULT_API_KEY_ENV,
        help="Env var holding the HTTP backend key.",
    )
    @click.option("--model", default=None, help="Provider-specific model.")
    @click.option("--temperature", type=float, default=None)
    @click.option("--top-p", type=float, default=None)
    @click.option(
        "--thinking",
        type=click.Choice(["high", "medium", "low", "none"]),
        default="none",
        help="Reasoning depth.",
    )
    @click.option("--num-ctx", type=int, default=None)
    @click.option(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help="Backend deadline in seconds.",
    )
    def cmd_run(
        output,
        input_,
        provider,
        mode,
        base_url,
        api_key_env,
        model,
        temperature,
        top_p,
        thinking,
        num_ctx,
        timeout,
    ):
        """Refine once and emit to the chosen sink."""
        input_mode = InputMode(input_)
        match input_mode:
            case InputMode.CLIPBOARD:
                input_adapter: InputAdapter = InputAdapterClipboard()
            case InputMode.STDIN:
                input_adapter = InputAdapterStdin()
            case _:
                raise click.UsageError(f"unknown input mode: {input_mode!r}")

        spec = EnrichSpec(
            provider=EnrichProvider(provider),
            model=model,
            mode=EnrichMode(mode),
            timeout=timeout,
            base_url=base_url,
            api_key_env=api_key_env,
            temperature=temperature,
            top_p=top_p,
            thinking=thinking,
            num_ctx=num_ctx,
            user_agent="copywriter/1.0",
        )
        enricher = build_enricher(
            spec, Copywriter.SYSTEM_PROMPT, Copywriter.USER_PROMPT
        )

        output_mode = OutputMode(output)
        match output_mode:
            case OutputMode.CLIPBOARD:
                output_adapter: OutputAdapter = OutputAdapterClipboard()
            case OutputMode.TYPE:
                output_adapter = OutputAdapterType()
            case OutputMode.STDOUT:
                output_adapter = OutputAdapterStdout()
            case _:
                raise click.UsageError(f"unknown output mode: {output_mode!r}")

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
