#!/usr/bin/env python3

import argparse
import json
import subprocess
import sys

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

def get_pipe_command(output_mode):
    """Get the pipe-to command for the specified output mode"""
    if output_mode == "clipboard":
        return ["wl-copy"]
    elif output_mode == "type":
        return ["ydotool", "type", "--file", "-"]
    else:
        raise ValueError(
            f"Invalid output mode: {output_mode}. Use 'clipboard' or 'type'"
        )

def start_speech(output_mode):
    """Start waystt with specified output mode"""
    if is_running():
        notify("Speech-to-text is already running")
        return False

    try:
        pipe_cmd = get_pipe_command(output_mode)

        # Start waystt in background
        # waystt --pipe-to takes multiple arguments: waystt --pipe-to command arg1 arg2...
        subprocess.Popen(
            ["waystt", "--pipe-to"] + pipe_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # Start a background process to monitor waystt and signal when it exits
        subprocess.Popen(
            [
                "sh",
                "-c",
                "while pgrep -x waystt >/dev/null; do sleep 0.5; done; waybar-signal.sh speech",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
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
        subprocess.run(["waybar-signal.sh", "speech"], check=False)
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
        subprocess.run(["waybar-signal.sh", "speech"], check=False)
        return True
    except Exception as e:
        notify(f"Failed to toggle recording: {e}")
        return False

def toggle_speech(output_mode):
    """Toggle speech recording or start if not running"""
    if is_running():
        toggle_recording()
    else:
        start_speech(output_mode)

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

    start_parser = subparsers.add_parser("start", help="Start speech-to-text")
    start_parser.add_argument(
        "output",
        choices=["clipboard", "type"],
        help="Output mode: 'clipboard' (wl-copy) or 'type' (ydotool)",
    )

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

    if args.command == "status":
        print(get_status_json())

    elif args.command == "is-recording":
        sys.exit(0 if is_running() else 1)

    elif args.command == "toggle":
        toggle_speech(args.output)

    elif args.command == "start":
        start_speech(args.output)

    elif args.command in ("stop", "kill"):
        stop_speech()

if __name__ == "__main__":
    main()
