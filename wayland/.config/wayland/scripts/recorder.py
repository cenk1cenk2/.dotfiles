#!/usr/bin/env -S sh -c 'exec uv run --project "$(dirname "$0")" "$0" "$@"'
# The `sh -c` trampoline lets us run `uv run --project <fixed dir>` no
# matter where the invoking shell's CWD happens to sit. See
# pyproject.toml for the full rationale.

import argparse
import json
import logging
import subprocess
import sys
import time

import psutil

from lib import notify, signal_waybar

# obsws-python + its websocket-client dependency log full tracebacks
# via `logger.exception()` whenever a connection is refused. waybar
# polls `recorder.py status` every tick, so a plain "OBS isn't
# running" state used to spam the compositor journal with two
# multi-line traceback blocks per second. Muting both loggers here
# (before we even try importing obsws-python) keeps the journal
# quiet; any real diagnostic goes through our own `log.debug` path.
logging.getLogger("obsws_python").setLevel(logging.CRITICAL)
logging.getLogger("websocket").setLevel(logging.CRITICAL)

try:
    import obsws_python as obs

    OBS_AVAILABLE = True
except ImportError:
    OBS_AVAILABLE = False

ICON = "/usr/share/icons/Adwaita/scalable/devices/camera-web.svg"
WAYBAR_MODULE = "recorder"
log = logging.getLogger("recorder")

class Recorder:
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
        elif cmd == "pause":
            self._pause()
        elif cmd == "open":
            self._open()
        elif cmd == "status":
            print(self._get_status_json())
        elif cmd == "is-recording":
            sys.exit(
                0 if (self._is_recording(silent=True) or self._is_obs_running()) else 1
            )

    def _notify(self, message, timeout=None):
        notify("Recording...", message, ICON, timeout)

    def _signal_waybar(self):
        signal_waybar(WAYBAR_MODULE)

    def _get_obs_connection(self, retry=3, wait=1, silent=False):
        """Open an obs-websocket connection. The `silent` path is
        tuned for waybar's `status` tick — no retries, no user-facing
        "couldn't connect" toast — because waybar polls constantly
        and OBS not being up is the common case, not an error.

        Interactive callers (`_start_recording`, `_stop_recording`)
        keep the default retry/wait so a transient start-up race
        doesn't abort a user-driven toggle."""
        if not OBS_AVAILABLE:
            if not silent:
                self._notify(
                    "obsws-python not installed. Install with: pip install obsws-python"
                )
            return None

        # Fast out when OBS isn't even running — saves a TCP RST
        # round-trip and keeps the websocket traceback noise out of
        # the journal for the common waybar idle tick.
        if not self._is_obs_running():
            if not silent:
                self._notify("OBS is not running")
            return None

        attempts = 1 if silent else max(1, retry)
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                return obs.ReqClient(host="localhost", port=4455, password="")
            except Exception as e:
                last_error = e
                if attempt < attempts - 1:
                    time.sleep(wait)
        if last_error is not None:
            if silent:
                log.debug("obs connection failed (silent): %s", last_error)
            else:
                self._notify(f"Failed to connect to OBS: {last_error}")

        return None

    def _is_obs_running(self):
        return any(p.info["name"] == "obs" for p in psutil.process_iter(["name"]))

    def _get_record_status(self, ws=None, silent=False):
        if not ws:
            ws = self._get_obs_connection(silent=silent)
        if not ws:
            return None

        try:
            return ws.get_record_status()
        except Exception as e:
            if silent:
                log.debug("get_record_status failed (silent): %s", e)
            return None

    def _is_recording(self, silent=False):
        status = self._get_record_status(silent=silent)

        return status.output_active if status else False

    def _start_recording(self):
        ws = self._get_obs_connection()
        if not ws:
            self._notify("Could not connect to OBS. Make sure OBS is running.")
            return False

        try:
            ws.start_record()
            self._signal_waybar()
            self._notify("Recording started")
            return True
        except Exception as e:
            self._notify(f"Failed to start recording: {e}")
            return False

    def _stop_recording(self):
        ws = self._get_obs_connection()
        if not ws:
            self._notify("Could not connect to OBS")
            return False

        try:
            try:
                status = ws.get_record_status()
                output_path = (
                    status.output_path if hasattr(status, "output_path") else None
                )
            except Exception:
                output_path = None

            ws.stop_record()
            for _ in range(25):
                time.sleep(0.2)
                status = self._get_record_status(ws=ws, silent=True)
                if not status or not status.output_active:
                    break
            self._signal_waybar()

            if output_path:
                self._notify(f"Recording saved to:\n{output_path}", timeout=5000)
            else:
                self._notify("Recording stopped.", timeout=3000)
            return True
        except Exception as e:
            self._notify(f"Failed to stop recording: {e}")
            return False

    def _toggle_pause(self):
        ws = self._get_obs_connection()
        if not ws:
            self._notify("Could not connect to OBS")
            return False

        try:
            ws.toggle_record_pause()
            self._signal_waybar()
            return True
        except Exception as e:
            self._notify(f"Failed to toggle pause: {e}")
            return False

    def _toggle(self):
        if self._is_recording():
            self._stop_recording()
        else:
            self._start_recording()

    def _start(self):
        if self._is_recording():
            self._notify("Recording already in progress.")
        else:
            self._start_recording()

    def _stop(self):
        if self._is_recording():
            self._stop_recording()
        else:
            self._notify("No recording in progress.")

    def _pause(self):
        if self._is_recording():
            self._toggle_pause()
        else:
            self._notify("No recording in progress.")

    def _open(self):
        if self._is_obs_running():
            self._notify("OBS is already running")
        else:
            subprocess.Popen(
                ["obs"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._notify("Opening OBS...", timeout=2000)
            self._signal_waybar()

    def _get_status_json(self):
        # Cheapest path first: if OBS isn't even up, skip the
        # websocket round-trip entirely. Waybar polls this frequently
        # and the common steady state is "idle", so opening a socket
        # every tick just to have it refused is wasted work (and
        # noisy — see the logger-muting block at module top).
        obs_up = self._is_obs_running()
        if not obs_up:
            return json.dumps(
                {"class": "idle", "text": "", "tooltip": "Not recording"}
            )

        status = self._get_record_status(silent=True)
        if status and status.output_active:
            paused = getattr(status, "output_paused", False)
            if paused:
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

        # OBS is running but either the websocket is unreachable
        # (auth config mismatch, plugin disabled) or it's simply not
        # recording yet. Either way the user action is the same —
        # click to start — so don't differentiate.
        return json.dumps(
            {
                "class": "ready",
                "text": "⏹",
                "tooltip": "OBS ready - Click to start recording",
            }
        )

def main():
    parser = argparse.ArgumentParser(
        description="Control OBS recording via WebSocket",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    subparsers = parser.add_subparsers(
        dest="command",
        help="Command to execute",
        required=True,
    )

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

    Recorder(args).run()

if __name__ == "__main__":
    main()
