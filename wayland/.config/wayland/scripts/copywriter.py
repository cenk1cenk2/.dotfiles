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

def notify(message, timeout=None):
    cmd = ["notify-send", "Copywriter", message, "-i", ICON]
    if timeout:
        cmd.extend(["-t", str(timeout)])
    subprocess.run(cmd)

def signal_waybar():
    subprocess.run(["waybar-signal.sh", "copywriter"], check=False)

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

def find_copywriter_processes():
    current = os.getpid()

    return [
        p
        for p in psutil.process_iter(["name", "cmdline"])
        if p.pid != current
        and p.info["cmdline"]
        and "copywriter.py" in " ".join(p.info["cmdline"])
        and "_run" in p.info["cmdline"]
    ]

def is_running():
    return len(find_copywriter_processes()) > 0

def get_clipboard():
    try:
        result = subprocess.run(
            ["wl-paste", "--no-newline"], capture_output=True, text=True, check=True
        )

        return result.stdout
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        log.error("failed to read clipboard: %s", e)

        return None

def run_http_completion(
    base_url,
    model,
    api_key,
    text,
    temperature=0.3,
    top_p=0.9,
    thinking=False,
    num_ctx=None,
):
    prompt = USER_PROMPT.format(text=text)
    body = {
        "model": model,
        "temperature": temperature,
        "top_p": top_p,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
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

    return data["choices"][0]["message"]["content"]

def run_refinement(
    provider,
    output_mode="clipboard",
    base_url=None,
    model=None,
    api_key=None,
    temperature=0.3,
    top_p=0.9,
    thinking=False,
    num_ctx=None,
):
    text = get_clipboard()
    if not text or not text.strip():
        notify("Clipboard is empty")

        return False

    log.info("clipboard text: %d chars", len(text))
    signal_waybar()
    result = None

    if provider == "http":
        log.info("sending to %s/chat/completions (model: %s)", base_url, model)
        try:
            result = run_http_completion(
                base_url,
                model,
                api_key,
                text,
                temperature=temperature,
                top_p=top_p,
                thinking=thinking,
                num_ctx=num_ctx,
            )
            log.info("refinement complete (%d chars)", len(result))
        except Exception as e:
            log.error("http completion failed: %s", e)
            notify(f"HTTP refinement failed: {e}")
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

    if not result or not result.strip():
        notify("Refinement failed, clipboard unchanged")

        return False

    output_cmd = get_output_command(output_mode)
    log.info("outputting to %s (%s)", output_mode, " ".join(output_cmd))
    subprocess.run(output_cmd, input=result.strip(), text=True)

    output_labels = {"clipboard": "clipboard", "type": "typing", "stdout": "stdout"}
    notify(f"Clipboard refined → {output_labels[output_mode]}", timeout=3000)
    log.info("done")

    return True

def get_state():
    if not is_running():
        return "idle"

    return "working"

def get_status_json():
    state = get_state()

    if state == "idle":
        return json.dumps({"class": "idle", "text": "", "tooltip": "Copywriter ready"})

    return json.dumps(
        {"class": "working", "text": "󰼭 󰧑", "tooltip": "Refining clipboard..."}
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
        default=0.3,
        help="Temperature for refinement (default: 0.3)",
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
    internal_parser.add_argument("--temperature", type=float, default=0.3)
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

    if args.command == "run":
        if is_running():
            notify("Copywriter is already running")

            return

        api_key = os.environ.get("AI_KILIC_DEV_API_KEY", "")

        cmd = [
            sys.executable,
            __file__,
            "_run",
            args.output,
            "--provider",
            args.provider,
            "--base-url",
            args.base_url,
            "--model",
            args.model,
            "--temperature",
            str(args.temperature),
            "--top-p",
            str(args.top_p),
            "--api-key",
            api_key,
        ]
        if args.thinking:
            cmd.append("--thinking")
        if args.num_ctx:
            cmd.extend(["--num-ctx", str(args.num_ctx)])

        if args.output == "stdout":
            subprocess.run(cmd)
            signal_waybar()
        else:
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            signal_waybar()
            output_labels = {"clipboard": "clipboard", "type": "typing"}
            notify(
                f"Refining clipboard → {output_labels[args.output]}...", timeout=2000
            )

    elif args.command == "kill":
        procs = find_copywriter_processes()
        if not procs:
            notify("Copywriter is not running")
            return
        for p in procs:
            log.info("killing copywriter process %d", p.pid)
            p.kill()
        notify("Copywriter killed")
        signal_waybar()

    elif args.command == "_run":
        run_refinement(
            args.provider,
            output_mode=args.output,
            base_url=args.base_url,
            model=args.model,
            api_key=args.api_key,
            temperature=args.temperature,
            top_p=args.top_p,
            thinking=args.thinking,
            num_ctx=args.num_ctx,
        )
        signal_waybar()

    elif args.command == "status":
        print(get_status_json())

    elif args.command == "is-running":
        sys.exit(0 if is_running() else 1)

if __name__ == "__main__":
    main()
