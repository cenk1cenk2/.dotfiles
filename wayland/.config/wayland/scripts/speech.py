#!/usr/bin/env python3

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request

DEFAULT_MODEL = "gemma4:31b-cloud"

log = logging.getLogger("speech")

ICON = "/usr/share/icons/Adwaita/scalable/devices/microphone.svg"
RECORDING_STATUS_FILE = os.path.expanduser("~/.config/hyprwhspr/recording_status")
RECORDING_CONTROL_FILE = os.path.expanduser("~/.config/hyprwhspr/recording_control")

def _load_system_prompt():
    with open(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "speech.md")
    ) as f:
        return f.read().strip()

AI_SYSTEM_PROMPT = _load_system_prompt()

AI_USER_PROMPT = "Clean up the following speech transcription:\n<transcription>\n{text}\n</transcription>"

class ClipboardWatcher:
    """Watches clipboard via wl-paste --watch and captures the latest value."""

    def __init__(self):
        self.captured = None
        self._proc = None
        self._thread = None

    def start(self):
        self._proc = subprocess.Popen(
            ["wl-paste", "--watch", "cat"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def _read_loop(self):
        buf = b""
        while self._proc and self._proc.poll() is None:
            chunk = self._proc.stdout.read(4096)
            if not chunk:
                break
            buf += chunk
            if b"\n" in chunk or len(chunk) < 4096:
                try:
                    self.captured = buf.decode("utf-8", errors="replace").strip()
                except Exception:
                    pass
                buf = b""

    def stop(self):
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None

    def get(self):
        return self.captured

class Speech:
    def __init__(self, args):
        self.args = args

    def run(self):
        cmd = self.args.command
        if cmd == "toggle":
            self._toggle()
        elif cmd in ("stop", "kill"):
            self._stop()
        elif cmd == "cancel":
            self._cancel()
        elif cmd == "status":
            print(self._get_status_json())
        elif cmd == "is-recording":
            sys.exit(0 if self._is_recording() else 1)

    def _notify(self, message, timeout=None):
        cmd = ["notify-send", "Speech-to-Text", message, "-i", ICON]
        if timeout:
            cmd.extend(["-t", str(timeout)])
        subprocess.run(cmd)

    def _signal_waybar(self):
        subprocess.run(["waybar-signal.sh", "speech"], check=False)

    def _is_recording(self):
        try:
            with open(RECORDING_STATUS_FILE) as f:
                return f.read().strip().lower() == "true"
        except FileNotFoundError:
            return False

    def _hyprwhspr_control(self, action):
        try:
            with open(RECORDING_CONTROL_FILE, "w") as f:
                f.write(action)
            log.info("hyprwhspr control: %s", action)
        except Exception as e:
            log.error("failed to write control file: %s", e)
            subprocess.run(
                ["hyprwhspr", "record", action],
                capture_output=True,
                text=True,
            )

    def _wait_for_idle(self, timeout=60):
        for _ in range(int(timeout / 0.1)):
            if not self._is_recording():
                return True
            time.sleep(0.1)

        return False

    def _enrich(self):
        if hasattr(self.args, "enrich") and self.args.enrich:
            return self.args.enrich_provider

        return None

    def _run_http_completion(self, transcription):
        prompt = AI_USER_PROMPT.format(text=transcription)

        body = {
            "model": self.args.enrich_model,
            "messages": [
                {"role": "system", "content": AI_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        }
        if self.args.enrich_temperature is not None:
            body["temperature"] = self.args.enrich_temperature
        if self.args.enrich_top_p is not None:
            body["top_p"] = self.args.enrich_top_p
        body["reasoning_effort"] = self.args.enrich_thinking
        if self.args.enrich_num_ctx:
            body["options"] = {"num_ctx": self.args.enrich_num_ctx}

        payload = json.dumps(body).encode()
        api_key = os.environ.get("AI_KILIC_DEV_API_KEY", "")

        req = urllib.request.Request(
            f"{self.args.enrich_base_url}/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "speech/1.0",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            error_body = e.read().decode(errors="replace")
            log.error("HTTP %d: %s", e.code, error_body)
            raise

        if not data or "choices" not in data or not data["choices"]:
            raise ValueError(f"unexpected API response: {data}")

        return data["choices"][0]["message"]["content"]

    def _run_ai_provider(self, transcription, provider):
        result = None

        if provider == "http":
            try:
                result = self._run_http_completion(transcription)
                log.info("enrichment complete (%d chars)", len(result))
            except Exception as e:
                log.error("http completion failed: %s", e)
                self._notify(f"HTTP enrichment failed: {e}")
        elif provider == "claude":
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
        elif provider == "codex":
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

        return result

    def _output_text(self, text, mode):
        if mode == "clipboard":
            subprocess.run(["wl-copy"], input=text, text=True)
        elif mode == "type":
            subprocess.run(
                [
                    "ydotool",
                    "type",
                    "--key-delay",
                    "10",
                    "--key-hold",
                    "10",
                    "--file",
                    "-",
                ],
                input=text,
                text=True,
            )

    def _toggle(self):
        output = self.args.output
        enrich = self._enrich()
        is_clipboard = output == "clipboard"
        needs_capture = is_clipboard or enrich

        if self._is_recording():
            log.info("stopping recording (output=%s, enrich=%s)", output, enrich)

            if needs_capture:
                watcher = ClipboardWatcher()
                watcher.start()
                time.sleep(0.05)

            self._hyprwhspr_control("stop")
            self._wait_for_idle(timeout=60)
            self._signal_waybar()

            if needs_capture:
                time.sleep(0.3)
                text = watcher.get()
                watcher.stop()

                if not text or not text.strip():
                    log.warning("no transcription captured from clipboard")
                    self._notify("No transcription captured")

                    return

                text = text.strip()
                log.info("captured %d chars from clipboard", len(text))

                if enrich:
                    self._notify("Enriching transcription...", timeout=3000)
                    enriched = self._run_ai_provider(text, enrich)
                    if enriched and enriched.strip():
                        text = enriched.strip()
                    else:
                        self._notify("Enrichment failed, using raw transcription")

                self._output_text(text, output)
                if enrich:
                    self._notify("Done")

            return

        log.info("starting recording (output=%s, enrich=%s)", output, enrich)
        self._hyprwhspr_control("start")
        self._signal_waybar()

    def _stop(self):
        self._hyprwhspr_control("stop")
        self._signal_waybar()

    def _cancel(self):
        self._hyprwhspr_control("cancel")
        self._signal_waybar()

    def _get_status_json(self):
        recording = self._is_recording()

        if not recording:
            return json.dumps(
                {"class": "idle", "text": "", "tooltip": "Speech-to-text ready"}
            )

        return json.dumps(
            {"class": "recording", "text": "󰍬", "tooltip": "Recording speech"}
        )

def _add_common_args(parser):
    parser.add_argument(
        "output",
        choices=["clipboard", "type"],
        help="Output mode: 'clipboard' (wl-copy) or 'type' (paste via hyprwhspr)",
    )
    parser.add_argument(
        "--enrich",
        action="store_true",
        help="Enrich transcription through AI",
    )
    parser.add_argument(
        "--enrich-provider",
        choices=["http", "claude", "codex"],
        default="http",
    )
    parser.add_argument(
        "--enrich-base-url",
        default="https://ai.kilic.dev/api/v1",
    )
    parser.add_argument("--enrich-model", default=DEFAULT_MODEL)
    parser.add_argument("--enrich-temperature", type=float)
    parser.add_argument("--enrich-top-p", type=float)
    parser.add_argument(
        "--enrich-thinking",
        nargs="?",
        const="high",
        default="none",
        choices=["high", "medium", "low", "none"],
    )
    parser.add_argument("--enrich-num-ctx", type=int)

def main():
    parser = argparse.ArgumentParser(description="Control hyprwhspr speech-to-text")
    parser.add_argument("-v", "--verbose", action="store_true")

    subparsers = parser.add_subparsers(dest="command", required=True)

    toggle_parser = subparsers.add_parser("toggle")
    _add_common_args(toggle_parser)

    subparsers.add_parser("stop")
    subparsers.add_parser("kill")
    subparsers.add_parser("cancel")
    subparsers.add_parser("status")
    subparsers.add_parser("is-recording")

    args = parser.parse_args()

    logging.basicConfig(
        format="%(name)s: %(message)s",
        level=logging.DEBUG if args.verbose else logging.WARNING,
    )

    Speech(args).run()

if __name__ == "__main__":
    main()
