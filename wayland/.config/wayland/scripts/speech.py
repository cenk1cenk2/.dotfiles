#!/usr/bin/env python3

import argparse
import json
import logging
import os
import subprocess
import sys
import urllib.request

import psutil

DEFAULT_MODEL = "gemma4:31b-cloud"

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
        elif cmd == "stop":
            self._stop()
        elif cmd == "kill":
            self._kill()
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

    def _is_daemon_recording(self):
        result = subprocess.run(
            ["hyprwhspr", "record", "status"],
            capture_output=True,
            text=True,
            check=False,
        )
        return "Recording in progress" in (result.stdout + result.stderr)

    def _is_recording(self):
        return self._get_speech_state() != "idle"

    def _find_toggle_processes(self):
        """Return live `speech.py toggle ...` processes (excluding self)."""
        procs = []
        my_pid = os.getpid()
        for p in psutil.process_iter(["pid", "cmdline"]):
            if p.info["pid"] == my_pid:
                continue
            try:
                cmdline = p.info["cmdline"] or []
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            if __file__ in cmdline and "toggle" in cmdline:
                procs.append(p)

        return procs

    def _get_output_mode(self):
        for proc in self._find_toggle_processes():
            try:
                cmdline = proc.cmdline()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            if "toggle" not in cmdline:
                continue
            idx = cmdline.index("toggle")
            if idx + 1 < len(cmdline) and cmdline[idx + 1] in ("clipboard", "type"):
                return cmdline[idx + 1]

        return "clipboard"

    def _get_enrich_provider(self):
        for proc in self._find_toggle_processes():
            try:
                cmdline = proc.cmdline()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            if "--enrich" not in cmdline:
                continue
            if "--enrich-provider" in cmdline:
                idx = cmdline.index("--enrich-provider")
                if idx + 1 < len(cmdline):
                    return cmdline[idx + 1]
            return "http"

        return None

    def _get_children_names(self):
        names = []
        for proc in self._find_toggle_processes():
            try:
                children = proc.children(recursive=True)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            for child in children:
                try:
                    names.append(child.name())
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

        return names

    def _hyprwhspr_control(self, action):
        log.info("hyprwhspr control: %s", action)
        subprocess.run(
            ["hyprwhspr", "record", action],
            capture_output=True,
            text=True,
            check=False,
        )

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
        if self._is_daemon_recording():
            # Press 2: daemon is already recording, so a capture subscriber
            # from press 1 is attached. Telling the daemon to stop finalises
            # the recording; it then routes the transcription to the
            # subscriber (main.py:1548), which wakes press 1 to do
            # enrich + output.
            log.info("daemon recording; requesting stop")
            self._hyprwhspr_control("stop")
            self._signal_waybar()

            return

        output = self.args.output
        enrich = self._enrich()
        log.info("starting session (output=%s, enrich=%s)", output, enrich)

        # Press 1: we are the session. `hyprwhspr record capture` connects to
        # the daemon's socket, triggers recording (or attaches to an in-flight
        # one), and blocks until the daemon closes the connection after
        # transcribing — which happens when press 2 sends stop. The daemon
        # writes the full transcription in one shot right before closing, so
        # communicate() gives us the text and the exit synchronously.
        capture = subprocess.Popen(
            ["hyprwhspr", "record", "capture"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self._signal_waybar()
        stdout, _ = capture.communicate()
        self._signal_waybar()

        text = stdout.decode("utf-8", errors="replace").strip() if stdout else ""
        if not text:
            log.warning("empty transcription from capture socket")
            self._notify("No transcription captured")

            return

        log.info("captured %d chars from socket", len(text))

        if enrich:
            if self.args.save:
                log.info("saving raw transcription to clipboard before enrichment")
                subprocess.run(["wl-copy"], input=text, text=True)
            self._notify("Enriching transcription...", timeout=3000)
            enriched = self._run_ai_provider(text, enrich)
            if enriched and enriched.strip():
                text = enriched.strip()
            else:
                self._notify("Enrichment failed, using raw transcription")

        self._signal_waybar()
        self._output_text(text, output)
        self._signal_waybar()
        if enrich:
            self._notify("Done")

    def _stop(self):
        self._hyprwhspr_control("stop")
        self._signal_waybar()

    def _kill(self):
        self._hyprwhspr_control("cancel")
        self._signal_waybar()

    def _get_speech_state(self):
        toggle_alive = bool(self._find_toggle_processes())
        daemon_rec = self._is_daemon_recording()

        if not toggle_alive and not daemon_rec:
            return "idle"
        if daemon_rec:
            return "recording"

        children = self._get_children_names()
        if any(c in ("wl-copy", "ydotool", "cat") for c in children):
            return "output"

        return "working"

    def _get_status_json(self):
        phase = self._get_speech_state()

        if phase == "idle":
            return json.dumps(
                {"class": "idle", "text": "", "tooltip": "Speech-to-text ready"}
            )

        mode = self._get_output_mode()
        icons = {"clipboard": "󰅇", "type": "󰌌"}
        labels = {"clipboard": "clipboard", "type": "typing"}
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
        text, tooltip = status_map.get(phase, status_map["recording"])

        return json.dumps({"class": phase, "text": text, "tooltip": tooltip})

def main():
    parser = argparse.ArgumentParser(description="Control hyprwhspr speech-to-text")
    parser.add_argument("-v", "--verbose", action="store_true")

    subparsers = parser.add_subparsers(dest="command", required=True)

    toggle_parser = subparsers.add_parser("toggle")
    toggle_parser.add_argument(
        "output",
        choices=["clipboard", "type"],
        help="Output mode: 'clipboard' (wl-copy) or 'type' (paste via hyprwhspr)",
    )
    toggle_parser.add_argument(
        "--enrich",
        action="store_true",
        help="Enrich transcription through AI",
    )
    toggle_parser.add_argument(
        "--enrich-provider",
        choices=["http", "claude", "codex"],
        default="http",
    )
    toggle_parser.add_argument(
        "--enrich-base-url",
        default="https://ai.kilic.dev/api/v1",
    )
    toggle_parser.add_argument("--enrich-model", default=DEFAULT_MODEL)
    toggle_parser.add_argument("--enrich-temperature", type=float)
    toggle_parser.add_argument("--enrich-top-p", type=float)
    toggle_parser.add_argument(
        "--enrich-thinking",
        nargs="?",
        const="high",
        default="none",
        choices=["high", "medium", "low", "none"],
    )
    toggle_parser.add_argument("--enrich-num-ctx", type=int)
    toggle_parser.add_argument(
        "--save",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Before AI enrichment, copy the raw transcription to the clipboard as a backup (default: True)",
    )

    subparsers.add_parser("stop")
    subparsers.add_parser("kill")
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
