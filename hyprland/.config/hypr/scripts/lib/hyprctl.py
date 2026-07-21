import glob
import json
import logging
import os
import socket
import subprocess
from typing import Any, Optional

log = logging.getLogger(__name__)

class Hyprctl:
    """Hyprland IPC over the request socket.

    Speaks the .socket.sock protocol directly (`j/monitors`,
    `dispatch ...`, `eval ...`) instead of forking a hyprctl process
    per call — same wire format hyprctl itself uses: one request per
    connection, response read to EOF.
    """

    def __init__(self) -> None:
        self._socket_path = self._resolve_socket()

    def _resolve_socket(self) -> Optional[str]:
        runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        signature = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
        if signature:
            path = os.path.join(runtime, "hypr", signature, ".socket.sock")
            return path if os.path.exists(path) else None
        # Bare-env callers (SSH, systemd): discover the first running
        # instance, mirroring `hyprctl -i 0`.
        candidates = sorted(glob.glob(os.path.join(runtime, "hypr", "*", ".socket.sock")))

        return candidates[0] if candidates else None

    def _request(self, message: str) -> Optional[str]:
        if not self._socket_path:
            log.debug("hyprland ipc: no socket found")
            return None
        log.debug("hyprland ipc: %s", message)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.connect(self._socket_path)
                sock.sendall(message.encode())
                chunks = []
                while chunk := sock.recv(8192):
                    chunks.append(chunk)
        except OSError as e:
            log.debug("hyprland ipc failed: %s", e)
            return None

        return b"".join(chunks).decode()

    def query(self, *args: str) -> Any:
        response = self._request("j/" + " ".join(args))
        if response is None:
            return None
        try:
            return json.loads(response)
        except json.JSONDecodeError:
            log.debug("hyprland ipc non-json response: %s", response[:200])
            return None

    def dispatch(self, expr: str) -> bool:
        """Call a Lua dispatcher expression. 0.55+ routes `dispatch`
        through `hl.dispatch(<expr>)`, so the legacy verb form
        (`dispatch movetoworkspace 5`) no longer works — pass the full
        expression here:
            hypr.dispatch('hl.dsp.window.move({ workspace = "5" })')"""
        response = self._request(f"dispatch {expr}")
        if response != "ok":
            log.debug("hyprland dispatch error: %s", response)

        return response == "ok"

    def eval(self, expr: str) -> bool:
        """Run an arbitrary Lua expression (e.g. `hl.config({...})`
        for keyword-style writes that aren't dispatcher calls)."""
        response = self._request(f"eval {expr}")
        if response != "ok":
            log.debug("hyprland eval error: %s", response)

        return response == "ok"

    def run_lua(self, script: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        """Run a Lua snippet with a plain interpreter, outside the
        compositor — for reading data out of the Lua config files
        (`eval` can't return values). The target file must be
        dofile-safe: no `hl` global exists here."""
        cmd = ["lua", "-e", script]
        merged = os.environ.copy()
        merged.update(env or {})
        log.debug("spawn: %s", " ".join(cmd))
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False, env=merged)
        if proc.stderr:
            log.debug("lua stderr: %s", proc.stderr.strip())

        return proc

    def monitors(self) -> list[dict[str, Any]]:
        return self.query("monitors") or []

    def clients(self) -> list[dict[str, Any]]:
        return self.query("clients") or []

    def workspaces(self) -> list[dict[str, Any]]:
        return self.query("workspaces") or []

    def active_window(self) -> Optional[dict[str, Any]]:
        result = self.query("activewindow")
        if not result or not result.get("address"):
            return None

        return result

    def active_workspace(self) -> Optional[dict[str, Any]]:
        return self.query("activeworkspace")

    def focused_monitor(self) -> Optional[dict[str, Any]]:
        for m in self.monitors():
            if m.get("focused"):
                return m

        return None

    def focused_workspace_id(self) -> Optional[int]:
        monitor = self.focused_monitor()
        if not monitor:
            return None

        return monitor.get("activeWorkspace", {}).get("id")
