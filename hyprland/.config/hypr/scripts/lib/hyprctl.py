import json
import subprocess
from typing import Any, Optional

class Hyprctl:
    def run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["hyprctl", *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def query(self, *args: str) -> Any:
        result = self.run("-j", *args)
        if result.returncode != 0:
            return None
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return None

    def dispatch(self, expr: str) -> bool:
        """Call a Lua dispatcher expression. 0.55 routes `hyprctl
        dispatch` through `hl.dispatch(<expr>)`, so the legacy verb
        form (`dispatch movetoworkspace 5`) no longer works — pass
        the full expression here:
            hypr.dispatch('hl.dsp.window.move({ workspace = "5" })')"""
        return self.run("dispatch", expr).returncode == 0

    def eval(self, expr: str) -> bool:
        """Run an arbitrary Lua expression (e.g. `hl.config({...})`
        for keyword-style writes that aren't dispatcher calls)."""
        return self.run("eval", expr).returncode == 0

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
