#!/usr/bin/env python3

import subprocess
import sys
import os
import time
from datetime import datetime

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

def countdown():
    """Show countdown notifications before recording"""
    for i in range(1, 4):  # seq 3 gives 1, 2, 3
        notify(f"Recording in {3 + 1 - i} seconds.", timeout=1000)
        time.sleep(1)

def is_recording():
    """Check if wl-screenrec is already running"""
    result = subprocess.run(["pgrep", "wl-screenrec"], capture_output=True)
    return result.returncode == 0

def kill_recording():
    """Stop any running recording"""
    subprocess.run(["killall", "-s", "SIGINT", "wl-screenrec"])
    subprocess.run(["waybar-signal.sh", "recorder"])
    notify("Recording stopped.")

def get_videos_dir():
    """Get the user's Videos directory"""
    try:
        result = subprocess.run(
            ["xdg-user-dir", "VIDEOS"], capture_output=True, text=True, check=True
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return os.path.expanduser("~/Videos")

def get_region_selection():
    """Get region selection using swaymsg, jq and slurp - exactly like the original"""
    try:
        # Use the exact same pipeline as the original script
        swaymsg_proc = subprocess.Popen(
            ["swaymsg", "-t", "get_tree"], stdout=subprocess.PIPE
        )
        jq_proc = subprocess.Popen(
            [
                "jq",
                "-r",
                '.. | select(.pid? and .visible?) | .rect | "\\(.x),\\(.y) \\(.width)x\\(.height)"',
            ],
            stdin=swaymsg_proc.stdout,
            stdout=subprocess.PIPE,
            text=True,
        )
        swaymsg_proc.stdout.close()

        slurp_result = subprocess.run(
            ["slurp"], stdin=jq_proc.stdout, capture_output=True, text=True, check=True
        )
        jq_proc.wait()
        return slurp_result.stdout.strip()
    except subprocess.CalledProcessError:
        return None

def main():
    if len(sys.argv) < 2:
        print("Usage: recorder.py <format> [region] [audio]")
        print("       recorder.py kill")
        sys.exit(1)

    # Check if already recording first (like original script)
    recording_status = is_recording()

    # Handle kill command
    if sys.argv[1] == "kill":
        kill_recording()
        sys.exit(0)
    elif recording_status:
        notify("Recording already in progress.")
        sys.exit(1)

    # Get parameters
    format_ext = sys.argv[1]
    region_mode = len(sys.argv) > 2 and sys.argv[2] == "region"
    audio_mode = len(sys.argv) > 3 and sys.argv[3] == "audio"

    # Setup file path
    target_path = get_videos_dir()
    timestamp = datetime.now().strftime("recording_%Y%m%d-%H%M%S")
    file_path = os.path.join(target_path, f"{timestamp}.{format_ext}")

    # Build command as string like the original
    command = f"wl-screenrec -f='{file_path}' --codec hevc"

    # Handle region selection
    if region_mode:
        notify("Select a region to record", timeout=1000)
        area = get_region_selection()
        if area:
            command = f"{command} -g '{area}'"
        else:
            notify("Failed to select region")
            sys.exit(1)

    # Handle audio
    if audio_mode:
        command = f"{command} --audio"

    # Start countdown
    countdown()

    # Start recording using shell=True like eval in original
    subprocess.run(command, shell=True)

    notify(f"Finished recording: {file_path}")

if __name__ == "__main__":
    main()
