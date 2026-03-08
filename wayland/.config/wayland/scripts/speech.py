#!/usr/bin/env python3

import argparse
import base64
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request

import psutil

DEFAULT_ENRICH_MODEL = "qwen2.5:14b-instruct"

log = logging.getLogger("speech")

def notify(
    message,
    timeout=None,
    icon="/usr/share/icons/Adwaita/scalable/devices/microphone.svg",
):
    """Send a notification using notify-send"""
    cmd = ["notify-send", "Speech-to-Text", message, "-i", icon]
    if timeout:
        cmd.extend(["-t", str(timeout)])
    subprocess.run(cmd)

def find_waystt_processes():
    return [p for p in psutil.process_iter(["name"]) if p.info["name"] == "waystt"]

def is_running():
    return len(find_waystt_processes()) > 0

def get_waystt_output_mode():
    for proc in find_waystt_processes():
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

def get_enrich_provider():
    for proc in find_waystt_processes():
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

def get_waystt_children():
    children = []
    for proc in find_waystt_processes():
        for child in proc.children(recursive=True):
            try:
                children.append(child.name())
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

    return children

def _get_focused_output():
    for cmd in [
        ["swaymsg", "-t", "get_outputs"],
        ["hyprctl", "monitors", "-j"],
    ]:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            for monitor in json.loads(result.stdout):
                if monitor.get("focused"):
                    return monitor["name"]
        except (subprocess.CalledProcessError, FileNotFoundError, json.JSONDecodeError):
            continue

    return None

def capture_screenshot():
    fd, path = tempfile.mkstemp(suffix=".png", prefix="speech-screenshot-")
    os.close(fd)
    try:
        cmd = ["grim"]
        output = _get_focused_output()
        if output:
            cmd.extend(["-o", output])
            log.info("capturing focused output: %s", output)
        subprocess.run(cmd + [path], check=True)
        log.info("screenshot captured: %s", path)

        return path
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        log.warning("screenshot capture failed: %s", e)
        os.unlink(path)

        return None

def _load_system_prompt():
    with open(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "speech.md")
    ) as f:
        return f.read().strip()

AI_SYSTEM_PROMPT = _load_system_prompt()

AI_USER_PROMPT = "Clean up the following speech transcription:\n<transcription>\n{text}\n</transcription>"

def get_output_command(output_mode):
    if output_mode == "stdout":
        return ["cat"]
    if output_mode == "clipboard":
        return ["wl-copy"]
    if output_mode == "type":
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
        f"Invalid output mode: {output_mode}. Use 'stdout', 'clipboard' or 'type'"
    )

def get_pipe_command(
    output_mode,
    enrich=None,
    enrich_base_url=None,
    enrich_model=None,
    enrich_temperature=None,
    enrich_top_p=None,
    enrich_thinking=False,
    screenshot_path=None,
    enrich_num_ctx=None,
):
    output_cmd = get_output_command(output_mode)

    if not enrich:
        return output_cmd

    cmd = [sys.executable, __file__, "_pipe-process", enrich, output_mode]
    if enrich_base_url:
        cmd.extend(["--enrich-base-url", enrich_base_url])
    if enrich_model:
        cmd.extend(["--enrich-model", enrich_model])
    if enrich_temperature is not None:
        cmd.extend(["--enrich-temperature", str(enrich_temperature)])
    if enrich_top_p is not None:
        cmd.extend(["--enrich-top-p", str(enrich_top_p)])
    if enrich_thinking:
        cmd.append("--enrich-thinking")
    if enrich_num_ctx:
        cmd.extend(["--enrich-num-ctx", str(enrich_num_ctx)])
    if enrich == "http":
        cmd.extend(["--api-key", os.environ.get("AI_KILIC_DEV_API_KEY", "")])
    if screenshot_path:
        cmd.extend(["--screenshot-path", screenshot_path])

    return cmd

def run_http_completion(
    base_url,
    model,
    api_key,
    transcription,
    temperature=0.3,
    top_p=0.9,
    thinking=False,
    screenshot_path=None,
    num_ctx=None,
):
    has_screenshot = screenshot_path and os.path.isfile(screenshot_path)

    prompt = AI_USER_PROMPT.format(text=transcription)
    if has_screenshot:
        prompt += (
            "\n\nREMINDER: The image above is SILENT CONTEXT ONLY. "
            "Do NOT describe or mention it. Output ONLY the cleaned transcription."
        )

    user_content = []
    if has_screenshot:
        with open(screenshot_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        user_content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}"},
            }
        )
        log.info("attached screenshot to request")
    user_content.append({"type": "text", "text": prompt})

    body = {
        "model": model,
        "temperature": temperature,
        "top_p": top_p,
        "messages": [
            {"role": "system", "content": AI_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
    }
    if thinking:
        body["chat_template_kwargs"] = {"enable_thinking": True}
        body["reasoning"] = {}
    if num_ctx:
        body["options"] = {"num_ctx": num_ctx}

    log.debug(
        "HTTP completion request: %s",
        json.dumps({**body, "messages": ["..."]}, indent=2),
    )
    payload = json.dumps(body).encode()

    req = urllib.request.Request(
        f"{base_url}/chat/completions",
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
        log.debug("HTTP error response body: %s", error_body)
        raise

    log.debug("HTTP completion response: %s", json.dumps(data, indent=2)[:2000])
    return data["choices"][0]["message"]["content"]

def run_pipe_processing(
    provider,
    output_mode,
    base_url=None,
    model=None,
    api_key=None,
    temperature=0.3,
    top_p=0.9,
    thinking=False,
    screenshot_path=None,
    num_ctx=None,
):
    log.info("reading transcription from stdin")
    transcription = sys.stdin.read()
    log.info("received %d chars from stdin", len(transcription))
    result = None

    if provider == "http":
        log.info("sending to %s/chat/completions (model: %s)", base_url, model)
        try:
            result = run_http_completion(
                base_url,
                model,
                api_key,
                transcription,
                temperature=temperature,
                top_p=top_p,
                thinking=thinking,
                screenshot_path=screenshot_path,
                num_ctx=num_ctx,
            )
            log.info("enrichment complete (%d chars)", len(result))
        except Exception as e:
            log.error("http completion failed: %s", e)
            notify(f"HTTP enrichment failed: {e}")
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

    if not result or not result.strip():
        log.warning("enrichment failed, falling back to raw transcription")
        notify("Enrichment failed, outputting raw transcription")
        result = transcription

    output_cmd = get_output_command(output_mode)
    log.info("outputting to %s (%s)", output_mode, " ".join(output_cmd))
    subprocess.run(output_cmd, input=result.strip(), text=True)

    if screenshot_path and os.path.isfile(screenshot_path):
        os.unlink(screenshot_path)
        log.info("cleaned up screenshot: %s", screenshot_path)

    log.info("done")

def signal_waybar():
    subprocess.run(["waybar-signal.sh", "speech"], check=False)

def wait_for_state(running, timeout=5):
    for _ in range(int(timeout / 0.25)):
        if is_running() == running:
            signal_waybar()
            return True
        time.sleep(0.25)

    return False

def start_speech(
    output_mode,
    stt_provider="http",
    stt_base_url=None,
    stt_model=None,
    enrich=None,
    enrich_base_url=None,
    enrich_model=None,
    enrich_temperature=None,
    enrich_top_p=None,
    enrich_thinking=False,
    enrich_num_ctx=None,
    screenshot=False,
):
    """Start waystt with specified output mode"""
    if is_running():
        notify("Speech-to-text is already running")
        return False

    screenshot_path = None
    if screenshot and enrich:
        screenshot_path = capture_screenshot()

    try:
        pipe_cmd = get_pipe_command(
            output_mode,
            enrich=enrich,
            enrich_base_url=enrich_base_url,
            enrich_model=enrich_model,
            enrich_temperature=enrich_temperature,
            enrich_top_p=enrich_top_p,
            enrich_thinking=enrich_thinking,
            screenshot_path=screenshot_path,
            enrich_num_ctx=enrich_num_ctx,
        )
        log.info("pipe command: %s", " ".join(pipe_cmd))

        env = None
        if stt_provider == "http":
            env = os.environ.copy()
            env["TRANSCRIPTION_PROVIDER"] = "openai"
            env["OPENAI_BASE_URL"] = stt_base_url or "https://ai.kilic.dev/api/v1"
            env["OPENAI_API_KEY"] = os.environ.get("AI_KILIC_DEV_API_KEY", "")
            if stt_model:
                env["WHISPER_MODEL"] = stt_model
            log.info(
                "remote stt: OPENAI_BASE_URL=%s WHISPER_MODEL=%s",
                env["OPENAI_BASE_URL"],
                stt_model or "(default)",
            )

        waystt_cmd = ["waystt", "--pipe-to"] + pipe_cmd
        log.info("waystt command: %s", " ".join(waystt_cmd))

        if output_mode == "stdout":
            log.info("running in synchronous/stdout mode")
            notify(
                "Speech-to-text started (output: stdout"
                + (f", enrich: {enrich}" if enrich else "")
                + ")"
            )
            subprocess.run(waystt_cmd, env=env)
            signal_waybar()
            log.info("stdout mode finished")

            return True

        log.info("starting waystt in background")
        # waystt --pipe-to takes multiple arguments: waystt --pipe-to command arg1 arg2...
        subprocess.Popen(
            waystt_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )

        if not wait_for_state(running=True):
            log.error("waystt did not start within timeout")
            notify("Failed to start speech-to-text: process did not start")
            return False

        log.info("waystt started, launching wait-and-signal watcher")
        subprocess.Popen(
            [sys.executable, __file__, "_wait-and-signal"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        output_desc = {"clipboard": "clipboard", "type": "typing"}[output_mode]
        enrich_desc = f", enrich: {enrich}" if enrich else ""
        notify(f"Speech-to-text started (output: {output_desc}{enrich_desc})")
        return True
    except Exception as e:
        notify(f"Failed to start speech-to-text: {e}")
        return False

def stop_speech():
    """Stop waystt process"""
    if not is_running():
        notify("Speech-to-text is not running")
        return False

    try:
        procs = find_waystt_processes()
        log.info("terminating %d waystt process(es)", len(procs))
        for proc in procs:
            proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=5)
            except psutil.TimeoutExpired:
                log.warning("process %d did not terminate, killing", proc.pid)
                proc.kill()
        signal_waybar()
        log.info("stopped")
        notify("Speech-to-text stopped")
        return True
    except Exception as e:
        notify(f"Failed to stop speech-to-text: {e}")
        return False

def toggle_recording():
    """Toggle recording on already-running waystt instance"""
    if not is_running():
        return False

    try:
        for proc in find_waystt_processes():
            proc.send_signal(signal.SIGUSR1)
        signal_waybar()

        return True
    except Exception as e:
        notify(f"Failed to toggle recording: {e}")
        return False

def toggle_speech(
    output_mode,
    stt_provider="http",
    stt_base_url=None,
    stt_model=None,
    enrich=None,
    enrich_base_url=None,
    enrich_model=None,
    enrich_temperature=None,
    enrich_top_p=None,
    enrich_thinking=False,
    enrich_num_ctx=None,
    screenshot=False,
):
    """Toggle speech recording or start if not running"""
    if is_running():
        log.info("waystt running, toggling recording")
        toggle_recording()
    else:
        log.info(
            "waystt not running, starting (output=%s, enrich=%s)", output_mode, enrich
        )
        start_speech(
            output_mode,
            stt_provider=stt_provider,
            stt_base_url=stt_base_url,
            stt_model=stt_model,
            enrich=enrich,
            enrich_base_url=enrich_base_url,
            enrich_model=enrich_model,
            enrich_temperature=enrich_temperature,
            enrich_top_p=enrich_top_p,
            enrich_thinking=enrich_thinking,
            enrich_num_ctx=enrich_num_ctx,
            screenshot=screenshot,
        )

def get_speech_state():
    if not is_running():
        return "idle"

    children = get_waystt_children()
    if any(c in ("cat", "wl-copy", "ydotool") for c in children):
        return "output"
    if any(c in ("claude", "node", "codex", "python3", "python") for c in children):
        return "working"

    return "recording"

def get_status_json():
    state = get_speech_state()

    if state == "idle":
        return json.dumps(
            {"class": "idle", "text": "", "tooltip": "Speech-to-text ready"}
        )

    mode = get_waystt_output_mode()
    icons = {"stdout": "󰞷", "clipboard": "󰅇", "type": "󰌌"}
    labels = {"stdout": "stdout", "clipboard": "clipboard", "type": "typing"}
    icon = icons.get(mode, "󰅇")
    label = labels.get(mode, mode)

    enrich = get_enrich_provider()
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
        "output": (icon, f"Outputting transcription{enrich_label} → {label}"),
    }
    text, tooltip = status_map[state]

    return json.dumps({"class": state, "text": text, "tooltip": tooltip})

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
    toggle_parser.add_argument(
        "output",
        choices=["stdout", "clipboard", "type"],
        help="Output mode: 'clipboard' (wl-copy) or 'type' (ydotool)",
    )
    # STT options
    toggle_parser.add_argument(
        "--stt-provider",
        choices=["http", "local"],
        default="http",
        help="STT provider (http: remote OpenAI-compatible, local: local whisper)",
    )
    toggle_parser.add_argument(
        "--stt-base-url",
        default="https://ai.kilic.dev/api/v1",
        help="Base URL for remote STT provider",
    )
    toggle_parser.add_argument(
        "--stt-model",
        default="",
        help="Whisper model for STT",
    )
    # Enrichment options
    toggle_parser.add_argument(
        "--enrich",
        action="store_true",
        help="Enrich transcription through AI to fix typos and improve readability",
    )
    toggle_parser.add_argument(
        "--enrich-provider",
        choices=["http", "claude", "codex"],
        default="http",
        help="AI provider for enrichment",
    )
    toggle_parser.add_argument(
        "--enrich-base-url",
        default="https://ai.kilic.dev/api/v1",
        help="Base URL for enrichment API",
    )
    toggle_parser.add_argument(
        "--enrich-model",
        default=DEFAULT_ENRICH_MODEL,
        help="Model to use for enrichment",
    )
    toggle_parser.add_argument(
        "--enrich-temperature",
        type=float,
        default=0.3,
        help="Temperature for enrichment (default: 0.3)",
    )
    toggle_parser.add_argument(
        "--enrich-top-p",
        type=float,
        default=0.9,
        help="Top-p for enrichment (default: 0.9)",
    )
    toggle_parser.add_argument(
        "--enrich-thinking",
        action="store_true",
        help="Enable model thinking/reasoning (default: disabled)",
    )
    toggle_parser.add_argument(
        "--enrich-num-ctx",
        type=int,
        default=16384,
        help="Context window size for ollama (default: 16384)",
    )
    toggle_parser.add_argument(
        "-s",
        "--screenshot",
        action="store_true",
        help="Capture focused screen as context for enrichment",
    )

    start_parser = subparsers.add_parser("start", help="Start speech-to-text")
    start_parser.add_argument(
        "output",
        choices=["stdout", "clipboard", "type"],
        help="Output mode: 'clipboard' (wl-copy) or 'type' (ydotool)",
    )
    # STT options
    start_parser.add_argument(
        "--stt-provider",
        choices=["http", "local"],
        default="http",
        help="STT provider (http: remote OpenAI-compatible, local: local whisper)",
    )
    start_parser.add_argument(
        "--stt-base-url",
        default="https://ai.kilic.dev/api/v1",
        help="Base URL for remote STT provider",
    )
    start_parser.add_argument(
        "--stt-model",
        default="distil-large-v3",
        help="Whisper model for STT",
    )
    # Enrichment options
    start_parser.add_argument(
        "--enrich",
        action="store_true",
        help="Enrich transcription through AI to fix typos and improve readability",
    )
    start_parser.add_argument(
        "--enrich-provider",
        choices=["http", "claude", "codex"],
        default="http",
        help="AI provider for enrichment",
    )
    start_parser.add_argument(
        "--enrich-base-url",
        default="https://ai.kilic.dev/api/v1",
        help="Base URL for enrichment API",
    )
    start_parser.add_argument(
        "--enrich-model",
        default=DEFAULT_ENRICH_MODEL,
        help="Model to use for enrichment",
    )
    start_parser.add_argument(
        "--enrich-temperature",
        type=float,
        default=0.3,
        help="Temperature for enrichment (default: 0.3)",
    )
    start_parser.add_argument(
        "--enrich-top-p",
        type=float,
        default=0.9,
        help="Top-p for enrichment (default: 0.9)",
    )
    start_parser.add_argument(
        "--enrich-thinking",
        action="store_true",
        help="Enable model thinking/reasoning (default: disabled)",
    )
    start_parser.add_argument(
        "--enrich-num-ctx",
        type=int,
        default=16384,
        help="Context window size for ollama (default: 16384)",
    )
    start_parser.add_argument(
        "-s",
        "--screenshot",
        action="store_true",
        help="Capture focused screen as context for enrichment",
    )

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
    enrich_process_parser.add_argument("--enrich-model", default=DEFAULT_ENRICH_MODEL)
    enrich_process_parser.add_argument("--enrich-temperature", type=float, default=0.3)
    enrich_process_parser.add_argument("--enrich-top-p", type=float, default=0.9)
    enrich_process_parser.add_argument("--enrich-thinking", action="store_true")
    enrich_process_parser.add_argument("--enrich-num-ctx", type=int, default=16384)
    enrich_process_parser.add_argument("--api-key", default="")
    enrich_process_parser.add_argument("--screenshot-path", default="")

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

    if args.command == "_pipe-process":
        run_pipe_processing(
            args.provider,
            args.output,
            base_url=args.enrich_base_url,
            model=args.enrich_model,
            api_key=args.api_key,
            temperature=args.enrich_temperature,
            top_p=args.enrich_top_p,
            thinking=args.enrich_thinking,
            screenshot_path=args.screenshot_path or None,
            num_ctx=args.enrich_num_ctx,
        )

    elif args.command == "_wait-and-signal":
        state_notifications = {
            "working": "Processing transcription...",
            "output": "Outputting transcription...",
        }
        last_state = None
        while is_running():
            state = get_speech_state()
            if state != last_state:
                signal_waybar()
                msg = state_notifications.get(state)
                if msg:
                    notify(msg, timeout=3000)
                last_state = state
            time.sleep(0.1)
        if last_state != "idle":
            signal_waybar()
            notify("Speech-to-text finished")

    elif args.command == "status":
        print(get_status_json())

    elif args.command == "is-recording":
        sys.exit(0 if is_running() else 1)

    elif args.command == "toggle":
        enrich = args.enrich_provider if args.enrich else None
        toggle_speech(
            args.output,
            stt_provider=args.stt_provider,
            stt_base_url=args.stt_base_url,
            stt_model=args.stt_model,
            enrich=enrich,
            enrich_base_url=args.enrich_base_url,
            enrich_model=args.enrich_model,
            enrich_temperature=args.enrich_temperature,
            enrich_top_p=args.enrich_top_p,
            enrich_thinking=args.enrich_thinking,
            enrich_num_ctx=args.enrich_num_ctx,
            screenshot=args.screenshot,
        )

    elif args.command == "start":
        enrich = args.enrich_provider if args.enrich else None
        start_speech(
            args.output,
            stt_provider=args.stt_provider,
            stt_base_url=args.stt_base_url,
            stt_model=args.stt_model,
            enrich=enrich,
            enrich_base_url=args.enrich_base_url,
            enrich_model=args.enrich_model,
            enrich_temperature=args.enrich_temperature,
            enrich_top_p=args.enrich_top_p,
            enrich_thinking=args.enrich_thinking,
            enrich_num_ctx=args.enrich_num_ctx,
            screenshot=args.screenshot,
        )

    elif args.command in ("stop", "kill"):
        stop_speech()

if __name__ == "__main__":
    main()
