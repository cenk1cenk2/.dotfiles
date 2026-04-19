#!/usr/bin/env python3

import argparse
import json
import logging
import os
import signal
import sys
from typing import Optional

import psutil

from lib import (
    DEFAULT_ENRICH_ADAPTER,
    EnrichAdapterClaude,
    InputAdapterClipboard,
    OutputAdapterClipboard,
    EnrichAdapterCodex,
    EnrichAdapter,
    EnrichProvider,
    EnrichAdapterHttp,
    InputAdapter,
    InputMode,
    OutputAdapter,
    OutputMode,
    InputAdapterStdin,
    OutputAdapterStdout,
    OutputAdapterType,
    load_prompt,
    notify,
    signal_waybar,
)

WAYBAR_MODULE = "copywriter"
ICON = "/usr/share/icons/Adwaita/symbolic/legacy/accessories-text-editor-symbolic.svg"

log = logging.getLogger("copywriter")

SYSTEM_PROMPT = load_prompt("copywriter.md", relative_to=__file__)
USER_PROMPT = "Clean up the following text:\n<text>\n{text}\n</text>"

class Copywriter:
    def __init__(
        self,
        args,
        input: Optional[InputAdapter] = None,
        enricher: Optional[EnrichAdapter] = None,
        output: Optional[OutputAdapter] = None,
    ):
        self.args = args
        self._input = input
        self._enricher = enricher
        self._output = output

    def run(self):
        cmd = self.args.command
        if cmd == "run":
            self._run()
        elif cmd == "kill":
            self._kill()
        elif cmd == "status":
            print(self._get_status_json())
        elif cmd == "is-running":
            sys.exit(0 if self._is_running() else 1)

    def _notify(self, message, timeout=None):
        notify("Copywriter", message, ICON, timeout)

    def _find_workers(self):
        """Live copywriter.py run processes, excluding self."""
        current = os.getpid()

        return [
            p
            for p in psutil.process_iter(["pid", "cmdline"])
            if p.info["pid"] != current
            and p.info["cmdline"]
            and __file__ in p.info["cmdline"]
            and "run" in p.info["cmdline"]
        ]

    def _is_running(self):
        return bool(self._find_workers())

    def _run(self):
        assert self._input is not None, "run requires an input adapter"
        assert self._enricher is not None, "run requires an enrich adapter"
        assert self._output is not None, "run requires an output adapter"

        if self._is_running():
            self._notify("Copywriter is already running")

            return

        # Synchronous for stdout (user wants it in their terminal); fork for
        # background sinks so the keybind returns fast. The child inherits
        # argv, so _find_workers keeps detecting it as the live worker.
        if self._output.mode is OutputMode.STDOUT:
            self._execute()
            signal_waybar(WAYBAR_MODULE)

            return

        pid = os.fork()
        if pid > 0:
            self._notify(
                f"Refining {self._input.mode.value} → {self._output.mode.value}...",
                timeout=2000,
            )
            signal_waybar(WAYBAR_MODULE)

            return

        os.setsid()
        try:
            self._execute()
        finally:
            signal_waybar(WAYBAR_MODULE)
            os._exit(0)

    def _execute(self):
        assert (
            self._input is not None
            and self._enricher is not None
            and self._output is not None
        )
        text = self._input.read()
        if not text or not text.strip():
            self._notify(f"{self._input.mode.value.capitalize()} is empty")

            return

        log.info("%s text: %d chars", self._input.mode.value, len(text))
        result = self._enricher.enrich(text)

        if not result or not result.strip():
            self._notify(
                f"Refinement failed, {self._output.mode.value} unchanged",
            )

            return

        self._output.write(result.strip())
        self._notify(
            f"Refined {self._input.mode.value} → {self._output.mode.value}",
            timeout=3000,
        )
        log.info("done")

    def _kill(self):
        workers = self._find_workers()
        if not workers:
            self._notify("Copywriter is not running")

            return

        # Each worker is its own session/group leader (see `os.setsid()` in
        # `_run`), so its PID doubles as a PGID. SIGKILL on the group takes
        # the worker AND any enrichment subprocess (claude/codex/wl-copy)
        # down together.
        for p in workers:
            log.info("killing copywriter worker group pgid=%d", p.pid)
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        self._notify("Copywriter killed")
        signal_waybar(WAYBAR_MODULE)

    def _get_status_json(self):
        if not self._is_running():
            return json.dumps(
                {"class": "idle", "text": "", "tooltip": "Copywriter ready"}
            )

        return json.dumps(
            {
                "class": "working",
                "text": "󰼭 󰧑",
                "tooltip": "Refining clipboard...",
            }
        )

def main():
    parser = argparse.ArgumentParser(description="Refine clipboard text through AI")
    parser.add_argument("-v", "--verbose", action="store_true")

    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument(
        "output",
        nargs="?",
        type=OutputMode,
        choices=list(OutputMode),
        default=OutputMode.CLIPBOARD,
        help="Output sink for the refined text",
    )
    run_parser.add_argument(
        "--input",
        dest="input",
        type=InputMode,
        choices=list(InputMode),
        default=InputMode.CLIPBOARD,
        help="Where to read the text from (default: clipboard)",
    )
    run_parser.add_argument(
        "--provider",
        type=EnrichProvider,
        choices=list(EnrichProvider),
        default=DEFAULT_ENRICH_ADAPTER,
    )
    run_parser.add_argument("--base-url", default="https://ai.kilic.dev/api/v1")
    # Per-provider default — each adapter picks its own when unset.
    run_parser.add_argument("--model", default=None)
    run_parser.add_argument("--temperature", type=float)
    run_parser.add_argument("--top-p", type=float)
    run_parser.add_argument(
        "--thinking",
        nargs="?",
        const="high",
        default="none",
        choices=["high", "medium", "low", "none"],
    )
    run_parser.add_argument("--num-ctx", type=int)

    subparsers.add_parser("kill")
    subparsers.add_parser("status")
    subparsers.add_parser("is-running")

    args = parser.parse_args()

    logging.basicConfig(
        format="%(name)s: %(message)s",
        level=logging.DEBUG if args.verbose else logging.WARNING,
    )

    input_adapter: Optional[InputAdapter] = None
    enricher: Optional[EnrichAdapter] = None
    output: Optional[OutputAdapter] = None
    if args.command == "run":
        match args.input:
            case InputMode.CLIPBOARD:
                input_adapter = InputAdapterClipboard()
            case InputMode.STDIN:
                input_adapter = InputAdapterStdin()
            case _:
                raise ValueError(f"unknown input mode: {args.input!r}")

        model_kw = {"model": args.model} if args.model else {}
        match args.provider:
            case EnrichProvider.HTTP:
                enricher = EnrichAdapterHttp(
                    SYSTEM_PROMPT,
                    USER_PROMPT,
                    base_url=args.base_url,
                    api_key=os.environ.get("AI_KILIC_DEV_API_KEY", ""),
                    temperature=args.temperature,
                    top_p=args.top_p,
                    thinking=args.thinking,
                    num_ctx=args.num_ctx,
                    user_agent="copywriter/1.0",
                    **model_kw,
                )
            case EnrichProvider.CLAUDE:
                enricher = EnrichAdapterClaude(SYSTEM_PROMPT, USER_PROMPT, **model_kw)
            case EnrichProvider.CODEX:
                enricher = EnrichAdapterCodex(SYSTEM_PROMPT, USER_PROMPT, **model_kw)
            case _:
                raise ValueError(f"unknown enrich provider: {args.provider!r}")

        match args.output:
            case OutputMode.CLIPBOARD:
                output = OutputAdapterClipboard()
            case OutputMode.TYPE:
                output = OutputAdapterType()
            case OutputMode.STDOUT:
                output = OutputAdapterStdout()
            case _:
                raise ValueError(f"unknown output mode: {args.output!r}")

    Copywriter(args, input_adapter, enricher, output).run()

if __name__ == "__main__":
    main()
