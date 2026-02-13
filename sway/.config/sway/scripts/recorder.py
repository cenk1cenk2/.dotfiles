#!/usr/bin/env python3

import argparse
import json
import subprocess
import sys
import time

import psutil

try:
    import obsws_python as obs

    OBS_AVAILABLE = True
except ImportError:
    OBS_AVAILABLE = False
    print("Warning: obsws-python not installed. Install with: pip install obsws-python")

def notify(
    message,
    timeout=None,
    icon="/usr/share/icons/Adwaita/scalable/devices/camera-web.svg",
):
    """Send a notification using notify-send"""
    cmd = ["notify-send", "Recording...", message, "-i", icon]
    if timeout:
        cmd.extend(["-t", str(timeout)])
    subprocess.run(cmd)

def get_obs_connection(retry=3, wait=1, silent=False):
    """Get OBS WebSocket connection with retry logic"""
    if not OBS_AVAILABLE:
        return None

    for attempt in range(retry):
        try:
            ws = obs.ReqClient(host="localhost", port=4455, password="")
            return ws
        except Exception as e:
            if attempt < retry - 1:
                time.sleep(wait)
            else:
                if not silent:
                    notify(f"Failed to connect to OBS: {e}")
                return None

    return None

def is_obs_running():
    return any(p.info["name"] == "obs" for p in psutil.process_iter(["name"]))

def get_record_status(silent=False):
    ws = get_obs_connection(silent=silent)
    if not ws:
        return None

    try:
        return ws.get_record_status()
    except Exception:
        return None

def is_recording(silent=False):
    status = get_record_status(silent=silent)

    return status.output_active if status else False

def is_paused(silent=False):
    status = get_record_status(silent=silent)

    return getattr(status, "output_paused", False) if status else False

def start_recording():
    """Start OBS recording via WebSocket"""
    ws = get_obs_connection()
    if not ws:
        notify("Could not connect to OBS. Make sure OBS is running.")
        return False

    try:
        ws.start_record()
        subprocess.run(["waybar-signal.sh", "recorder"])
        notify("Recording started")
        return True
    except Exception as e:
        notify(f"Failed to start recording: {e}")
        return False

def stop_recording():
    """Stop OBS recording via WebSocket"""
    ws = get_obs_connection()
    if not ws:
        notify("Could not connect to OBS")
        return False

    try:
        # Get the current recording status before stopping to get the output path
        try:
            status = ws.get_record_status()
            output_path = status.output_path if hasattr(status, "output_path") else None
        except:
            output_path = None

        ws.stop_record()
        for _ in range(25):
            time.sleep(0.2)
            status = get_record_status(silent=True)
            if not status or not status.output_active:
                break
        subprocess.run(["waybar-signal.sh", "recorder"])

        # Show where the file was saved
        if output_path:
            notify(f"Recording saved to:\n{output_path}", timeout=5000)
        else:
            notify("Recording stopped.", timeout=3000)
        return True
    except Exception as e:
        notify(f"Failed to stop recording: {e}")
        return False

def toggle_pause():
    """Toggle pause/resume OBS recording"""
    ws = get_obs_connection()
    if not ws:
        notify("Could not connect to OBS")
        return False

    try:
        ws.toggle_record_pause()
        subprocess.run(["waybar-signal.sh", "recorder"])
        return True
    except Exception as e:
        notify(f"Failed to toggle pause: {e}")
        return False

def get_status_json():
    status = get_record_status(silent=True)

    if status and status.output_active:
        if getattr(status, "output_paused", False):
            return json.dumps(
                {
                    "class": "recording paused",
                    "text": "⏸",
                    "tooltip": "Recording paused - Click to toggle",
                }
            )

        return json.dumps(
            {
                "class": "recording",
                "text": "⏺",
                "tooltip": "Recording active - Click to stop",
            }
        )

    if is_obs_running():
        return json.dumps(
            {
                "class": "ready",
                "text": "⏹",
                "tooltip": "OBS ready - Click to start recording",
            }
        )

    return json.dumps({"class": "idle", "text": "", "tooltip": "Not recording"})

def open_obs():
    """Open OBS GUI"""
    if is_obs_running():
        notify("OBS is already running")
    else:
        subprocess.Popen(
            ["obs"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        notify("Opening OBS...", timeout=2000)
        subprocess.run(["waybar-signal.sh", "recorder"])

def main():
    parser = argparse.ArgumentParser(
        description="Control OBS recording via WebSocket",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    subparsers.add_parser("toggle", help="Toggle recording (start/stop)")
    subparsers.add_parser("start", help="Start recording")
    subparsers.add_parser("stop", help="Stop recording")
    subparsers.add_parser("pause", help="Toggle pause/resume")
    subparsers.add_parser("open", help="Open OBS GUI")
    subparsers.add_parser("status", help="Get recording status (JSON for waybar)")
    subparsers.add_parser(
        "is-recording", help="Check if recording (exit code 0 if yes)"
    )
    subparsers.add_parser("kill", help="Stop recording (alias for 'stop')")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "status":
        print(get_status_json())

    elif args.command == "is-recording":
        sys.exit(0 if (is_recording(silent=True) or is_obs_running()) else 1)

    elif args.command == "toggle":
        if is_recording():
            stop_recording()
        else:
            start_recording()

    elif args.command == "start":
        if is_recording():
            notify("Recording already in progress.")
        else:
            start_recording()

    elif args.command in ("stop", "kill"):
        if is_recording():
            stop_recording()
        else:
            notify("No recording in progress.")

    elif args.command == "pause":
        if is_recording():
            toggle_pause()
        else:
            notify("No recording in progress.")

    elif args.command == "open":
        open_obs()

if __name__ == "__main__":
    main()
