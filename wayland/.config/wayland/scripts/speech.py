#!/usr/bin/env python3

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import time
import urllib.request

import psutil

DEFAULT_MODEL = "gemma3:27b-cloud"

log = logging.getLogger("speech")

ICON = "/usr/share/icons/Adwaita/scalable/devices/microphone.svg"

def _load_system_prompt():
    with open(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "speech.md")
    ) as f:
        return f.read().strip()

AI_SYSTEM_PROMPT = _load_system_prompt()

AI_USER_PROMPT = "Clean up the following speech transcription:\n<transcription>\n{text}\n</transcription>"

class Speech:
    def __init__(self, args):
        self.args = args

    def run(self):
        cmd = self.args.command
        if cmd == "toggle":
            self._toggle()
        elif cmd == "start":
            self._start()
        elif cmd in ("stop", "kill"):
            self._stop()
        elif cmd == "status":
            print(self._get_status_json())
        elif cmd == "is-recording":
            sys.exit(0 if self._is_running() else 1)
        elif cmd == "_pipe-process":
            self._pipe_process()
        elif cmd == "_wait-and-signal":
            self._wait_and_signal()

    def _notify(self, message, timeout=None):
        cmd = ["notify-send", "Speech-to-Text", message, "-i", ICON]
        if timeout:
            cmd.extend(["-t", str(timeout)])
        subprocess.run(cmd)

    def _signal_waybar(self):
        subprocess.run(["waybar-signal.sh", "speech"], check=False)

    def _find_processes(self):
        return [p for p in psutil.process_iter(["name"]) if p.info["name"] == "waystt"]

    def _is_running(self):
        return len(self._find_processes()) > 0

    def _get_output_mode(self):
        for proc in self._find_processes():
            try:
                cmdline = proc.cmdline()
                if any("ydotool" in arg for arg in cmdline):
                    return "type"
                if "_pipe-process" in cmdline:
                    idx = cmdline.index("_pipe-process")
                    if idx + 2 < len(cmdline) and cmdline[idx + 2] in (
                        "stdout",
                        "clipboard",
                        "type",
                    ):
                        return cmdline[idx + 2]
                if "stdout" in cmdline:
                    return "stdout"
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        return "clipboard"

    def _get_enrich_provider(self):
        for proc in self._find_processes():
            try:
                cmdline = proc.cmdline()
                cmdline_str = " ".join(cmdline)
                if "_pipe-process" in cmdline_str:
                    for i, arg in enumerate(cmdline):
                        if arg == "_pipe-process" and i + 1 < len(cmdline):
                            return cmdline[i + 1]
                if "claude" in cmdline_str:
                    return "claude"
                if "codex" in cmdline_str:
                    return "codex"
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        return None

    def _get_children(self):
        children = []
        for proc in self._find_processes():
            for child in proc.children(recursive=True):
                try:
                    children.append(child.name())
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

        return children

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

    def _enrich(self):
        if hasattr(self.args, "enrich") and self.args.enrich:
            return self.args.enrich_provider

        return None

    def _run_http_completion(self, transcription):
        prompt = AI_USER_PROMPT.format(text=transcription)

        body = {
            "model": self.args.enrich_model,
            "temperature": self.args.enrich_temperature,
            "top_p": self.args.enrich_top_p,
            "messages": [
                {"role": "system", "content": AI_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        }
        if self.args.enrich_thinking:
            body["chat_template_kwargs"] = {"enable_thinking": True}
            body["reasoning"] = {}
        if self.args.enrich_num_ctx:
            body["options"] = {"num_ctx": self.args.enrich_num_ctx}

        log.debug(
            "HTTP completion request: %s",
            json.dumps({**body, "messages": ["..."]}, indent=2),
        )
        payload = json.dumps(body).encode()

        req = urllib.request.Request(
            f"{self.args.enrich_base_url}/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.args.api_key}",
                "User-Agent": "speech/1.0",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            error_body = e.read().decode(errors="replace")
            log.error("HTTP %d: %s", e.code, error_body)
            log.debug("HTTP error response body: %s", error_body)
            raise

        log.debug("HTTP completion response: %s", json.dumps(data, indent=2)[:2000])
        if not data or "choices" not in data or not data["choices"]:
            raise ValueError(f"unexpected API response: {data}")

        return data["choices"][0]["message"]["content"]

    def _run_ai_provider(self, transcription):
        provider = self.args.provider
        result = None

        if provider == "http":
            log.info(
                "sending to %s/chat/completions (model: %s)",
                self.args.enrich_base_url,
                self.args.enrich_model,
            )
            try:
                result = self._run_http_completion(transcription)
                log.info("enrichment complete (%d chars)", len(result))
            except Exception as e:
                log.error("http completion failed: %s", e)
                self._notify(f"HTTP enrichment failed: {e}")
        elif provider == "claude":
            log.info("sending to claude (model: haiku)")
            prompt = AI_USER_PROMPT.format(text=transcription)
            ai_proc = subprocess.run(
                [
                    "claude",
                    "-p",
                    "--model",
                    "haiku",
                    "--system-prompt",
                    AI_SYSTEM_PROMPT,
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
            prompt = AI_USER_PROMPT.format(text=transcription)
            codex_prompt = f"{AI_SYSTEM_PROMPT}\n\n{prompt}"
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

    def _get_pipe_command(self):
        enrich = self._enrich()
        if not enrich:
            return self._get_output_command()

        cmd = [sys.executable, __file__]
        if self.args.verbose:
            cmd.append("-v")
        cmd += ["_pipe-process", enrich, self.args.output]
        if self.args.enrich_base_url:
            cmd.extend(["--enrich-base-url", self.args.enrich_base_url])
        if self.args.enrich_model:
            cmd.extend(["--enrich-model", self.args.enrich_model])
        if self.args.enrich_temperature is not None:
            cmd.extend(["--enrich-temperature", str(self.args.enrich_temperature)])
        if self.args.enrich_top_p is not None:
            cmd.extend(["--enrich-top-p", str(self.args.enrich_top_p)])
        if self.args.enrich_thinking:
            cmd.append("--enrich-thinking")
        if self.args.enrich_num_ctx:
            cmd.extend(["--enrich-num-ctx", str(self.args.enrich_num_ctx)])
        if self.args.save:
            cmd.append("--save")
        if enrich == "http":
            cmd.extend(["--api-key", os.environ.get("AI_KILIC_DEV_API_KEY", "")])

        return cmd

    def _build_stt_env(self):
        if self.args.stt_provider != "http":
            return None

        env = os.environ.copy()
        env["TRANSCRIPTION_PROVIDER"] = "openai"
        env["OPENAI_BASE_URL"] = self.args.stt_base_url or "https://ai.kilic.dev/api/v1"
        env["OPENAI_API_KEY"] = os.environ.get("AI_KILIC_DEV_API_KEY", "")
        if self.args.stt_model:
            env["WHISPER_MODEL"] = self.args.stt_model
        log.info(
            "remote stt: OPENAI_BASE_URL=%s WHISPER_MODEL=%s",
            env["OPENAI_BASE_URL"],
            self.args.stt_model or "(default)",
        )

        return env

    def _wait_for_state(self, running, timeout=5):
        for _ in range(int(timeout / 0.25)):
            if self._is_running() == running:
                self._signal_waybar()

                return True
            time.sleep(0.25)

        return False

    def _toggle(self):
        if self._is_running():
            log.info("waystt running, toggling recording")
            self._toggle_recording()
        else:
            enrich = self._enrich()
            log.info(
                "waystt not running, starting (output=%s, enrich=%s)",
                self.args.output,
                enrich,
            )
            self._start()

    def _start(self):
        if self._is_running():
            self._notify("Speech-to-text is already running")

            return False

        try:
            pipe_cmd = self._get_pipe_command()
            log.info("pipe command: %s", " ".join(pipe_cmd))

            env = self._build_stt_env()

            waystt_cmd = ["waystt", "--pipe-to"] + pipe_cmd
            log.info("waystt command: %s", " ".join(waystt_cmd))

            if self.args.output == "stdout":
                log.info("running in synchronous/stdout mode")
                enrich = self._enrich()
                self._notify(
                    "Speech-to-text started (output: stdout"
                    + (f", enrich: {enrich}" if enrich else "")
                    + ")"
                )
                subprocess.run(waystt_cmd, env=env)
                self._signal_waybar()
                log.info("stdout mode finished")

                return True

            log.info("starting waystt in background")
            subprocess.Popen(
                waystt_cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
            )

            if not self._wait_for_state(running=True):
                log.error("waystt did not start within timeout")
                self._notify("Failed to start speech-to-text: process did not start")

                return False

            log.info("waystt started, launching wait-and-signal watcher")
            subprocess.Popen(
                [sys.executable, __file__, "_wait-and-signal"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

            output_desc = {"clipboard": "clipboard", "type": "typing"}[self.args.output]
            enrich = self._enrich()
            enrich_desc = f", enrich: {enrich}" if enrich else ""
            self._notify(f"Speech-to-text started (output: {output_desc}{enrich_desc})")

            return True
        except Exception as e:
            self._notify(f"Failed to start speech-to-text: {e}")

            return False

    def _stop(self):
        if not self._is_running():
            self._notify("Speech-to-text is not running")

            return False

        try:
            procs = self._find_processes()
            log.info("terminating %d waystt process(es)", len(procs))
            for proc in procs:
                proc.terminate()
            for proc in procs:
                try:
                    proc.wait(timeout=5)
                except psutil.TimeoutExpired:
                    log.warning("process %d did not terminate, killing", proc.pid)
                    proc.kill()
            self._signal_waybar()
            log.info("stopped")
            self._notify("Speech-to-text stopped")

            return True
        except Exception as e:
            self._notify(f"Failed to stop speech-to-text: {e}")

            return False

    def _toggle_recording(self):
        if not self._is_running():
            return False

        try:
            for proc in self._find_processes():
                proc.send_signal(signal.SIGUSR1)
            self._signal_waybar()

            return True
        except Exception as e:
            self._notify(f"Failed to toggle recording: {e}")

            return False

    def _pipe_process(self):
        log.info("reading transcription from stdin")
        transcription = sys.stdin.read()
        log.info("received %d chars from stdin", len(transcription))

        if self.args.save:
            log.info("saving raw transcription to clipboard before enrichment")
            subprocess.run(["wl-copy"], input=transcription.strip(), text=True)

        result = self._run_ai_provider(transcription)

        if not result or not result.strip():
            log.warning("enrichment failed, falling back to raw transcription")
            self._notify("Enrichment failed, outputting raw transcription")
            result = transcription

        output_cmd = self._get_output_command()
        log.info("outputting to %s (%s)", self.args.output, " ".join(output_cmd))
        subprocess.run(output_cmd, input=result.strip(), text=True)

        log.info("done")

    def _wait_and_signal(self):
        state_notifications = {
            "working": "Processing transcription...",
            "output": "Outputting transcription...",
        }
        last_state = None
        while self._is_running():
            state = self._get_speech_state()
            if state != last_state:
                self._signal_waybar()
                msg = state_notifications.get(state)
                if msg:
                    self._notify(msg, timeout=3000)
                last_state = state
            time.sleep(0.1)
        if last_state != "idle":
            self._signal_waybar()
            self._notify("Speech-to-text finished")

    def _get_speech_state(self):
        if not self._is_running():
            return "idle"

        children = self._get_children()
        if any(c in ("cat", "wl-copy", "ydotool") for c in children):
            return "output"
        if any(c in ("claude", "node", "codex", "python3", "python") for c in children):
            return "working"

        return "recording"

    def _get_status_json(self):
        state = self._get_speech_state()

        if state == "idle":
            return json.dumps(
                {"class": "idle", "text": "", "tooltip": "Speech-to-text ready"}
            )

        mode = self._get_output_mode()
        icons = {"stdout": "󰞷", "clipboard": "󰅇", "type": "󰌌"}
        labels = {"stdout": "stdout", "clipboard": "clipboard", "type": "typing"}
        icon = icons.get(mode, "󰅇")
        label = labels.get(mode, mode)

        enrich = self._get_enrich_provider()
        enrich_icon = " 󰧑" if enrich else ""
        enrich_label = f" ({enrich})" if enrich else ""

        status_map = {
            "recording": (
                f"󰍬{enrich_icon} {icon}",
                f"Recording speech{enrich_label} → {label}",
            ),
            "working": (
                f"󰍬{enrich_icon} {icon}",
                f"Processing transcription{enrich_label} → {label}",
            ),
            "output": (
                icon,
                f"Outputting transcription{enrich_label} → {label}",
            ),
        }
        text, tooltip = status_map[state]

        return json.dumps({"class": state, "text": text, "tooltip": tooltip})

def _add_common_args(parser):
    parser.add_argument(
        "output",
        choices=["stdout", "clipboard", "type"],
        help="Output mode: 'clipboard' (wl-copy) or 'type' (ydotool)",
    )
    parser.add_argument(
        "--stt-provider",
        choices=["http", "local"],
        default="http",
        help="STT provider (http: remote OpenAI-compatible, local: local whisper)",
    )
    parser.add_argument(
        "--stt-base-url",
        default="https://ai.kilic.dev/api/v1",
        help="Base URL for remote STT provider",
    )
    parser.add_argument(
        "--stt-model",
        default="",
        help="Whisper model for STT",
    )
    parser.add_argument(
        "--enrich",
        action="store_true",
        help="Enrich transcription through AI to fix typos and improve readability",
    )
    parser.add_argument(
        "--enrich-provider",
        choices=["http", "claude", "codex"],
        default="http",
        help="AI provider for enrichment",
    )
    parser.add_argument(
        "--enrich-base-url",
        default="https://ai.kilic.dev/api/v1",
        help="Base URL for enrichment API",
    )
    parser.add_argument(
        "--enrich-model",
        default=DEFAULT_MODEL,
        help="Model to use for enrichment",
    )
    parser.add_argument(
        "--enrich-temperature",
        type=float,
        default=0.6,
        help="Temperature for enrichment (default: 0.5)",
    )
    parser.add_argument(
        "--enrich-top-p",
        type=float,
        default=0.9,
        help="Top-p for enrichment (default: 0.9)",
    )
    parser.add_argument(
        "--enrich-thinking",
        action="store_true",
        help="Enable model thinking/reasoning (default: disabled)",
    )
    parser.add_argument(
        "--enrich-num-ctx",
        type=int,
        help="Context window size for ollama",
    )
    parser.add_argument(
        "--save",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Save transcription to clipboard before AI enrichment (default: True)",
    )

def main():
    parser = argparse.ArgumentParser(
        description="Control waystt speech-to-text",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="verbose logging")

    subparsers = parser.add_subparsers(
        dest="command", help="Command to execute", required=True
    )

    toggle_parser = subparsers.add_parser(
        "toggle",
        help="Toggle recording (start if not running, or send SIGUSR1 if running)",
    )
    _add_common_args(toggle_parser)

    start_parser = subparsers.add_parser("start", help="Start speech-to-text")
    _add_common_args(start_parser)
    start_parser.set_defaults(stt_model="distil-large-v3")

    enrich_process_parser = subparsers.add_parser(
        "_pipe-process", help=argparse.SUPPRESS
    )
    enrich_process_parser.add_argument("provider", choices=["http", "claude", "codex"])
    enrich_process_parser.add_argument(
        "output", choices=["stdout", "clipboard", "type"]
    )
    enrich_process_parser.add_argument(
        "--enrich-base-url", default="https://ai.kilic.dev/api/v1"
    )
    enrich_process_parser.add_argument("--enrich-model", default=DEFAULT_MODEL)
    enrich_process_parser.add_argument("--enrich-temperature", type=float, default=0.6)
    enrich_process_parser.add_argument("--enrich-top-p", type=float, default=0.9)
    enrich_process_parser.add_argument("--enrich-thinking", action="store_true")
    enrich_process_parser.add_argument("--enrich-num-ctx", type=int)
    enrich_process_parser.add_argument("--api-key", default="")
    enrich_process_parser.add_argument("--save", action="store_true")

    subparsers.add_parser("_wait-and-signal", help=argparse.SUPPRESS)
    subparsers.add_parser("stop", help="Stop waystt process")
    subparsers.add_parser("kill", help="Stop waystt process (alias for 'stop')")
    subparsers.add_parser("status", help="Get speech-to-text status (JSON for waybar)")
    subparsers.add_parser(
        "is-recording", help="Check if waystt is running (exit code 0 if yes)"
    )

    args = parser.parse_args()

    logging.basicConfig(
        format="%(name)s: %(message)s",
        level=logging.DEBUG if args.verbose else logging.WARNING,
    )

    Speech(args).run()

if __name__ == "__main__":
    main()
