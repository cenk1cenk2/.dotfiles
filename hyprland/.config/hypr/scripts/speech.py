#!/usr/bin/env python3

import argparse
import json
import subprocess
import sys
import time

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

def is_running():
    """Check if waystt is running"""
    result = subprocess.run(["pgrep", "-x", "waystt"], capture_output=True)
    return result.returncode == 0

AI_PROMPT = (
    "You are a speech-to-text post-processor. The following text was transcribed "
    "from spoken audio and likely contains recognition errors, missing punctuation, "
    "and awkward phrasing. Clean it up: fix typos and misrecognized words, add proper "
    "punctuation and capitalization, improve sentence structure while preserving the "
    "original meaning and tone. Format the output using proper markdown: use "
    "lists, code blocks, and emphasis where appropriate based on the content. "
    "Use headings only if the text is long enough to warrant them. "
    "Output ONLY the corrected and formatted text with no commentary."
)

def get_pipe_command(output_mode, ai=False):
    """Get the pipe-to command for the specified output mode"""
    if output_mode == "clipboard":
        output_cmd = "wl-copy"
    elif output_mode == "type":
        output_cmd = "ydotool type --key-delay 5 --key-hold 5 --file -"
    else:
        raise ValueError(
            f"Invalid output mode: {output_mode}. Use 'clipboard' or 'type'"
        )

    if ai:
        prompt = AI_PROMPT.replace("'", "'\\''")
        return [
            "sh",
            "-c",
            f"claude -p --model haiku '{prompt}' | {output_cmd}",
        ]

    if output_mode == "clipboard":
        return ["wl-copy"]

    return ["ydotool", "type", "--key-delay", "5", "--key-hold", "5", "--file", "-"]

def signal_waybar():
    subprocess.run(["waybar-signal.sh", "speech"], check=False)

def wait_for_state(running, timeout=5):
    for _ in range(int(timeout / 0.25)):
        if is_running() == running:
            signal_waybar()
            return True
        time.sleep(0.25)

    return False

def start_speech(output_mode, ai=False):
    """Start waystt with specified output mode"""
    if is_running():
        notify("Speech-to-text is already running")
        return False

    try:
        pipe_cmd = get_pipe_command(output_mode, ai=ai)

        # Start waystt in background
        # waystt --pipe-to takes multiple arguments: waystt --pipe-to command arg1 arg2...
        subprocess.Popen(
            ["waystt", "--pipe-to"] + pipe_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        if not wait_for_state(running=True):
            notify("Failed to start speech-to-text: process did not start")
            return False

        subprocess.Popen(
            [sys.executable, __file__, "_wait-and-signal"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        output_desc = "clipboard" if output_mode == "clipboard" else "typing"
        notify(f"Speech-to-text started (output: {output_desc})")
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
        subprocess.run(["pkill", "-x", "waystt"], check=True)
        wait_for_state(running=False)
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
        subprocess.run(["pkill", "--signal", "SIGUSR1", "waystt"], check=True)
        signal_waybar()

        return True
    except Exception as e:
        notify(f"Failed to toggle recording: {e}")
        return False

def toggle_speech(output_mode, ai=False):
    """Toggle speech recording or start if not running"""
    if is_running():
        toggle_recording()
    else:
        start_speech(output_mode, ai=ai)

def get_status_json():
    """Get speech-to-text status as JSON for waybar"""
    if is_running():
        return json.dumps(
            {
                "class": "recording",
                "text": "🎤",
                "tooltip": "Speech-to-text active - Click to toggle",
            }
        )

    return json.dumps(
        {
            "class": "idle",
            "text": "",
            "tooltip": "Speech-to-text ready",
        }
    )

def main():
    parser = argparse.ArgumentParser(
        description="Control waystt speech-to-text",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    subparsers = parser.add_subparsers(
        dest="command", help="Command to execute", required=True
    )

    toggle_parser = subparsers.add_parser(
        "toggle",
        help="Toggle recording (start if not running, or send SIGUSR1 if running)",
    )
    toggle_parser.add_argument(
        "output",
        choices=["clipboard", "type"],
        help="Output mode: 'clipboard' (wl-copy) or 'type' (ydotool)",
    )
    toggle_parser.add_argument(
        "--ai",
        action="store_true",
        help="Pipe text through Claude AI to fix typos and improve readability",
    )

    start_parser = subparsers.add_parser("start", help="Start speech-to-text")
    start_parser.add_argument(
        "output",
        choices=["clipboard", "type"],
        help="Output mode: 'clipboard' (wl-copy) or 'type' (ydotool)",
    )
    start_parser.add_argument(
        "--ai",
        action="store_true",
        help="Pipe text through Claude AI to fix typos and improve readability",
    )

    subparsers.add_parser("_wait-and-signal", help=argparse.SUPPRESS)
    subparsers.add_parser("stop", help="Stop waystt process")
    subparsers.add_parser("kill", help="Stop waystt process (alias for 'stop')")
    subparsers.add_parser("status", help="Get speech-to-text status (JSON for waybar)")
    subparsers.add_parser(
        "is-recording", help="Check if waystt is running (exit code 0 if yes)"
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "_wait-and-signal":
        while is_running():
            time.sleep(0.5)
        signal_waybar()

    elif args.command == "status":
        print(get_status_json())

    elif args.command == "is-recording":
        sys.exit(0 if is_running() else 1)

    elif args.command == "toggle":
        toggle_speech(args.output, ai=args.ai)

    elif args.command == "start":
        start_speech(args.output, ai=args.ai)

    elif args.command in ("stop", "kill"):
        stop_speech()

if __name__ == "__main__":
    main()
