#!/usr/bin/env -S sh -c 'exec uv run --project "$(dirname "$0")" "$0" "$@"'

from __future__ import annotations

import json
import logging
import subprocess
import sys
import time

import click
import psutil
import obsws_python as obs
from lib import configure_logging, notify, signal_waybar

# obsws-python + websocket-client log full tracebacks via `logger.exception()`
# on every refused connection. Waybar polls status on a tick, so without
# muting these the journal fills up when OBS is down.
logging.getLogger("obsws_python").setLevel(logging.CRITICAL)
logging.getLogger("websocket").setLevel(logging.CRITICAL)

class Recorder:
    WAYBAR_MODULE = "recorder"
    ICON = "/usr/share/icons/Adwaita/scalable/devices/camera-web.svg"

    log = logging.getLogger("recorder")

    def _notify(self, message, timeout=None):
        notify("Recording...", message, self.ICON, timeout)

    def _signal_waybar(self):
        signal_waybar(self.WAYBAR_MODULE)

    def _is_obs_running(self) -> bool:
        return any(p.info["name"] == "obs" for p in psutil.process_iter(["name"]))

    def _connection(self, *, retry=3, wait=1, silent=False):
        """Open an obs-websocket client.

        `silent=True` drops retries + notifications for waybar's status
        tick path, where OBS being down is the expected state."""
        if not self._is_obs_running():
            if not silent:
                self._notify("OBS is not running")
            return None

        attempts = 1 if silent else max(1, retry)
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                self.log.debug("obs.ReqClient attempt=%d", attempt + 1)
                return obs.ReqClient(host="localhost", port=4455, password="")
            except Exception as e:
                last_error = e
                if attempt < attempts - 1:
                    time.sleep(wait)
        if last_error is not None:
            self.log.debug("obs connection failed: %s", last_error)
            if not silent:
                self._notify(f"Failed to connect to OBS: {last_error}")
        return None

    def _record_status(self, ws=None, silent=False):
        if not ws:
            ws = self._connection(silent=silent)
        if not ws:
            return None
        try:
            return ws.get_record_status()
        except Exception as e:
            self.log.debug("get_record_status failed: %s", e)
            return None

    def is_recording(self, silent: bool = False) -> bool:
        status = self._record_status(silent=silent)
        return bool(status.output_active) if status else False

    def start(self) -> bool:
        ws = self._connection()
        if not ws:
            self._notify("Could not connect to OBS. Make sure OBS is running.")
            return False
        try:
            self.log.info("start_record")
            ws.start_record()
            self._signal_waybar()
            self._notify("Recording started")
            return True
        except Exception as e:
            self.log.error("start_record failed: %s", e)
            self._notify(f"Failed to start recording: {e}")
            return False

    def stop(self) -> bool:
        ws = self._connection()
        if not ws:
            self._notify("Could not connect to OBS")
            return False
        try:
            try:
                output_path = getattr(ws.get_record_status(), "output_path", None)
            except Exception:
                output_path = None

            self.log.info("stop_record")
            ws.stop_record()
            for _ in range(25):
                time.sleep(0.2)
                status = self._record_status(ws=ws, silent=True)
                if not status or not status.output_active:
                    break
            self._signal_waybar()

            if output_path:
                self._notify(f"Recording saved to:\n{output_path}", timeout=5000)
            else:
                self._notify("Recording stopped.", timeout=3000)
            return True
        except Exception as e:
            self.log.error("stop_record failed: %s", e)
            self._notify(f"Failed to stop recording: {e}")
            return False

    def pause(self) -> bool:
        ws = self._connection()
        if not ws:
            self._notify("Could not connect to OBS")
            return False
        try:
            ws.toggle_record_pause()
            self._signal_waybar()
            return True
        except Exception as e:
            self.log.error("toggle_record_pause failed: %s", e)
            self._notify(f"Failed to toggle pause: {e}")
            return False

    def open(self) -> None:
        if self._is_obs_running():
            self._notify("OBS is already running")
            return
        self.log.info("spawning obs")
        subprocess.Popen(["obs"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._notify("Opening OBS...", timeout=2000)
        self._signal_waybar()

    def status_json(self) -> str:
        if not self._is_obs_running():
            return json.dumps({"class": "idle", "text": "", "tooltip": "Not recording"})
        status = self._record_status(silent=True)
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
        return json.dumps(
            {
                "class": "ready",
                "text": "⏹",
                "tooltip": "OBS ready - Click to start recording",
            }
        )

    # ── CLI ───────────────────────────────────────────────────────

    @click.group(context_settings={"help_option_names": ["-h", "--help"]})
    @click.option("-v", "--verbose", is_flag=True, help="Enable debug logging.")
    def cli(verbose: bool):
        """Control OBS recording via WebSocket."""
        configure_logging(verbose)

    @cli.command("toggle")
    def cmd_toggle():
        """Toggle recording start/stop."""
        rec = Recorder()
        if rec.is_recording():
            rec.stop()
        else:
            rec.start()

    @cli.command("start")
    def cmd_start():
        """Start recording."""
        rec = Recorder()
        if rec.is_recording():
            rec._notify("Recording already in progress.")
        else:
            rec.start()

    @cli.command("stop")
    def cmd_stop():
        """Stop recording."""
        rec = Recorder()
        if rec.is_recording():
            rec.stop()
        else:
            rec._notify("No recording in progress.")

    @cli.command("pause")
    def cmd_pause():
        """Toggle recording pause."""
        rec = Recorder()
        if rec.is_recording():
            rec.pause()
        else:
            rec._notify("No recording in progress.")

    @cli.command("open")
    def cmd_open():
        """Launch the OBS GUI."""
        Recorder().open()

    @cli.command("status")
    def cmd_status():
        """Print waybar-shaped status JSON."""
        sys.stdout.write(Recorder().status_json() + "\n")

    @cli.command("is-recording")
    def cmd_is_recording():
        """Exit 0 if recording or OBS is up."""
        rec = Recorder()
        sys.exit(0 if (rec.is_recording(silent=True) or rec._is_obs_running()) else 1)

    @cli.command("kill")
    def cmd_kill():
        """Alias for stop."""
        rec = Recorder()
        if rec.is_recording():
            rec.stop()
        else:
            rec._notify("No recording in progress.")

if __name__ == "__main__":
    Recorder.cli()
