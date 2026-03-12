#!/usr/bin/env python3

import argparse
import json
import logging
import os
import subprocess
import sys
import urllib.request

import psutil

DEFAULT_MODEL = "gemma3:27b-cloud"

log = logging.getLogger("copywriter")

ICON = "/usr/share/icons/Adwaita/symbolic/legacy/accessories-text-editor-symbolic.svg"

def _load_system_prompt():
    with open(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "copywriter.md")
    ) as f:
        return f.read().strip()

SYSTEM_PROMPT = _load_system_prompt()

USER_PROMPT = "Clean up the following text:\n<text>\n{text}\n</text>"

class Copywriter:
    def __init__(self, args):
        self.args = args

    def run(self):
        cmd = self.args.command
        if cmd == "run":
            self._run_command()
        elif cmd == "_run":
            self._run_internal()
            self._signal_waybar()
        elif cmd == "kill":
            self._kill()
        elif cmd == "status":
            print(self._get_status_json())
        elif cmd == "is-running":
            sys.exit(0 if self._is_running() else 1)

    def _notify(self, message, timeout=None):
        cmd = ["notify-send", "Copywriter", message, "-i", ICON]
        if timeout:
            cmd.extend(["-t", str(timeout)])
        subprocess.run(cmd)

    def _signal_waybar(self):
        subprocess.run(["waybar-signal.sh", "copywriter"], check=False)

    def _find_processes(self):
        current = os.getpid()

        return [
            p
            for p in psutil.process_iter(["name", "cmdline"])
            if p.pid != current
            and p.info["cmdline"]
            and "copywriter.py" in " ".join(p.info["cmdline"])
            and "_run" in p.info["cmdline"]
        ]

    def _is_running(self):
        return len(self._find_processes()) > 0

    def _get_clipboard(self):
        try:
            result = subprocess.run(
                ["wl-paste", "--no-newline"],
                capture_output=True,
                text=True,
                check=True,
            )

            return result.stdout
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            log.error("failed to read clipboard: %s", e)

            return None

    def _get_output_command(self):
        mode = self.args.output
        if mode == "stdout":
            return ["cat"]
        if mode == "clipboard":
            return ["wl-copy"]
        if mode == "type":
            return [
                "ydotool",
                "type",
                "--key-delay",
                "10",
                "--key-hold",
                "10",
                "--file",
                "-",
            ]

        raise ValueError(
            f"Invalid output mode: {mode}. Use 'stdout', 'clipboard' or 'type'"
        )

    def _run_http_completion(self, text):
        prompt = USER_PROMPT.format(text=text)
        body = {
            "model": self.args.model,
            "temperature": self.args.temperature,
            "top_p": self.args.top_p,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        }
        if self.args.thinking:
            body["chat_template_kwargs"] = {"enable_thinking": True}
            body["reasoning"] = {}
        if self.args.num_ctx:
            body["options"] = {"num_ctx": self.args.num_ctx}

        log.debug(
            "HTTP completion request: %s",
            json.dumps({**body, "messages": ["..."]}, indent=2),
        )
        payload = json.dumps(body).encode()

        req = urllib.request.Request(
            f"{self.args.base_url}/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.args.api_key}",
                "User-Agent": "copywriter/1.0",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            error_body = e.read().decode(errors="replace")
            log.error("HTTP %d: %s", e.code, error_body)
            raise

        log.debug("HTTP completion response: %s", json.dumps(data, indent=2)[:2000])
        if not data or "choices" not in data or not data["choices"]:
            raise ValueError(f"unexpected API response: {data}")

        return data["choices"][0]["message"]["content"]

    def _run_ai_provider(self, text):
        provider = self.args.provider
        result = None

        if provider == "http":
            log.info(
                "sending to %s/chat/completions (model: %s)",
                self.args.base_url,
                self.args.model,
            )
            try:
                result = self._run_http_completion(text)
                log.info("refinement complete (%d chars)", len(result))
            except Exception as e:
                log.error("http completion failed: %s", e)
                self._notify(f"HTTP refinement failed: {e}")
        elif provider == "claude":
            log.info("sending to claude (model: haiku)")
            prompt = USER_PROMPT.format(text=text)
            ai_proc = subprocess.run(
                [
                    "claude",
                    "-p",
                    "--model",
                    "haiku",
                    "--system-prompt",
                    SYSTEM_PROMPT,
                    prompt,
                ],
                capture_output=True,
                text=True,
            )
            if ai_proc.returncode == 0 and ai_proc.stdout.strip():
                result = ai_proc.stdout.strip()
                log.info("claude refinement complete (%d chars)", len(result))
            else:
                log.error("claude failed (exit %d)", ai_proc.returncode)
        elif provider == "codex":
            log.info("sending to codex")
            prompt = USER_PROMPT.format(text=text)
            codex_prompt = f"{SYSTEM_PROMPT}\n\n{prompt}"
            ai_proc = subprocess.run(
                ["codex", "exec", "-", "--ephemeral", "--skip-git-repo-check"],
                input=codex_prompt,
                capture_output=True,
                text=True,
            )
            if ai_proc.returncode == 0 and ai_proc.stdout.strip():
                result = ai_proc.stdout.strip()
                log.info("codex refinement complete (%d chars)", len(result))
            else:
                log.error("codex failed (exit %d)", ai_proc.returncode)

        return result

    def _build_internal_cmd(self):
        api_key = os.environ.get("AI_KILIC_DEV_API_KEY", "")
        cmd = [sys.executable, __file__]
        if self.args.verbose:
            cmd.append("-v")
        cmd += [
            "_run",
            self.args.output,
            "--provider",
            self.args.provider,
            "--base-url",
            self.args.base_url,
            "--model",
            self.args.model,
            "--temperature",
            str(self.args.temperature),
            "--top-p",
            str(self.args.top_p),
            "--api-key",
            api_key,
        ]
        if self.args.thinking:
            cmd.append("--thinking")
        if self.args.num_ctx:
            cmd.extend(["--num-ctx", str(self.args.num_ctx)])

        return cmd

    def _run_command(self):
        if self._is_running():
            self._notify("Copywriter is already running")

            return

        cmd = self._build_internal_cmd()

        if self.args.output == "stdout":
            subprocess.run(cmd)
            self._signal_waybar()
        else:
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._signal_waybar()
            output_labels = {"clipboard": "clipboard", "type": "typing"}
            self._notify(
                f"Refining clipboard → {output_labels[self.args.output]}...",
                timeout=2000,
            )

    def _run_internal(self):
        text = self._get_clipboard()
        if not text or not text.strip():
            self._notify("Clipboard is empty")

            return

        log.info("clipboard text: %d chars", len(text))
        self._signal_waybar()

        result = self._run_ai_provider(text)

        if not result or not result.strip():
            self._notify("Refinement failed, clipboard unchanged")

            return

        output_cmd = self._get_output_command()
        log.info("outputting to %s (%s)", self.args.output, " ".join(output_cmd))
        subprocess.run(output_cmd, input=result.strip(), text=True)

        output_labels = {
            "clipboard": "clipboard",
            "type": "typing",
            "stdout": "stdout",
        }
        self._notify(
            f"Clipboard refined → {output_labels[self.args.output]}",
            timeout=3000,
        )
        log.info("done")

    def _kill(self):
        procs = self._find_processes()
        if not procs:
            self._notify("Copywriter is not running")

            return
        for p in procs:
            log.info("killing copywriter process %d", p.pid)
            p.kill()
        self._notify("Copywriter killed")
        self._signal_waybar()

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
    parser = argparse.ArgumentParser(
        description="Refine clipboard text through AI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="verbose logging")

    subparsers = parser.add_subparsers(
        dest="command", help="Command to execute", required=True
    )

    run_parser = subparsers.add_parser("run", help="Refine clipboard text through AI")
    run_parser.add_argument(
        "output",
        nargs="?",
        choices=["stdout", "clipboard", "type"],
        default="clipboard",
        help="Output mode: 'clipboard' (wl-copy), 'type' (ydotool) or 'stdout'",
    )
    run_parser.add_argument(
        "--provider",
        choices=["http", "claude", "codex"],
        default="http",
        help="AI provider for refinement",
    )
    run_parser.add_argument(
        "--base-url",
        default="https://ai.kilic.dev/api/v1",
        help="Base URL for HTTP provider",
    )
    run_parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help="Model to use for refinement",
    )
    run_parser.add_argument(
        "--temperature",
        type=float,
        default=0.6,
        help="Temperature for refinement (default: 0.5)",
    )
    run_parser.add_argument(
        "--top-p",
        type=float,
        default=0.9,
        help="Top-p for refinement (default: 0.9)",
    )
    run_parser.add_argument(
        "--thinking",
        action="store_true",
        help="Enable model thinking/reasoning",
    )
    run_parser.add_argument(
        "--num-ctx",
        type=int,
        help="Context window size for ollama ",
    )

    # Internal subcommand for background execution
    internal_parser = subparsers.add_parser("_run", help=argparse.SUPPRESS)
    internal_parser.add_argument("output", default="clipboard")
    internal_parser.add_argument("--provider", default="http")
    internal_parser.add_argument("--base-url", default="https://ai.kilic.dev/api/v1")
    internal_parser.add_argument("--model", default=DEFAULT_MODEL)
    internal_parser.add_argument("--temperature", type=float, default=0.6)
    internal_parser.add_argument("--top-p", type=float, default=0.9)
    internal_parser.add_argument("--thinking", action="store_true")
    internal_parser.add_argument("--num-ctx", type=int)
    internal_parser.add_argument("--api-key", default="")

    subparsers.add_parser("kill", help="Kill running copywriter process")
    subparsers.add_parser("status", help="Get status (JSON for waybar)")
    subparsers.add_parser("is-running", help="Check if running (exit code 0 if yes)")

    args = parser.parse_args()

    logging.basicConfig(
        format="%(name)s: %(message)s",
        level=logging.DEBUG if args.verbose else logging.WARNING,
    )

    Copywriter(args).run()

if __name__ == "__main__":
    main()
