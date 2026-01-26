#!/usr/bin/env python3

import subprocess
import sys
import time

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

def get_obs_connection(retry=3, wait=1):
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
                notify(f"Failed to connect to OBS: {e}")
                return None

    return None

def is_obs_running():
    """Check if OBS is running"""
    result = subprocess.run(["pgrep", "obs"], capture_output=True)
    return result.returncode == 0

def is_recording():
    """Check if OBS is recording"""
    ws = get_obs_connection()
    if not ws:
        return False

    try:
        status = ws.get_record_status()
        return status.output_active
    except Exception:
        return False

def start_recording():
    """Start OBS recording via WebSocket"""
    ws = get_obs_connection()
    if not ws:
        notify("Could not connect to OBS. Make sure OBS is running.")
        return False

    try:
        ws.start_record()
        subprocess.run(["pkill", "-RTMIN+8", "waybar"])
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
        subprocess.run(["pkill", "-RTMIN+8", "waybar"])

        # Show where the file was saved
        if output_path:
            notify(f"Recording saved to:\n{output_path}", timeout=5000)
        else:
            notify("Recording stopped.", timeout=3000)
        return True
    except Exception as e:
        notify(f"Failed to stop recording: {e}")
        return False

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

def main():
    if len(sys.argv) < 2:
        print("Usage: recorder.py <command>")
        print("Commands:")
        print("  toggle  - Toggle recording (start/stop)")
        print("  start   - Start recording")
        print("  stop    - Stop recording")
        print("  open    - Open OBS")
        sys.exit(1)

    command = sys.argv[1]

    if command == "toggle":
        if is_recording():
            stop_recording()
        else:
            start_recording()

    elif command == "start":
        if is_recording():
            notify("Recording already in progress.")
        else:
            start_recording()

    elif command == "stop":
        if is_recording():
            stop_recording()
        else:
            notify("No recording in progress.")

    elif command == "open":
        open_obs()

    # Keep "kill" as alias for "stop" for backwards compatibility
    elif command == "kill":
        if is_recording():
            stop_recording()
        else:
            notify("No recording in progress.")

    else:
        notify(f"Unknown command: {command}")
        sys.exit(1)

if __name__ == "__main__":
    main()
