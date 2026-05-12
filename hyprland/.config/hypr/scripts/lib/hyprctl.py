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

    def dispatch(self, verb: str, *args: str) -> bool:
        """Hyprland 0.55 in Lua mode routes `hyprctl dispatch` through
        the Lua state, so the legacy `dispatch <verb> <args>` form is
        a syntax error. Translate the verbs we use into hl.dsp.*
        expressions; fall back to passing raw args for anything we
        don't recognise (which will fail loudly under Lua mode)."""
        expr = self._translate_dispatch(verb, args)
        if expr is None:
            return self.run("dispatch", verb, *args).returncode == 0
        return self.run("dispatch", expr).returncode == 0

    def keyword(self, name: str, value: str) -> bool:
        """Same story as dispatch: `hyprctl keyword` is rejected under
        Lua mode (`keyword can't work with non-legacy parsers. Use
        eval.`). Build an equivalent `hl.config({...})` expression."""
        return self.run("dispatch", self._translate_keyword(name, value)).returncode == 0

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

    @staticmethod
    def _lua_str(s: str) -> str:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

    @classmethod
    def _translate_dispatch(cls, verb: str, args: tuple[str, ...]) -> Optional[str]:
        if verb == "cyclenext":
            if not args:
                return "hl.dsp.window.cycle_next()"
            if args[0] == "tiled":
                return "hl.dsp.window.cycle_next({ tiled = true })"
            if args[0] == "floating":
                return "hl.dsp.window.cycle_next({ floating = true })"
            return None

        if verb == "focuswindow" and args:
            return f"hl.dsp.focus({{ window = {cls._lua_str(args[0])} }})"

        if verb == "workspace" and args:
            return f"hl.dsp.focus({{ workspace = {cls._lua_str(args[0])} }})"

        if verb in ("movetoworkspace", "movetoworkspacesilent") and args:
            ws, _, sel = args[0].partition(",")
            fields = [f"workspace = {cls._lua_str(ws)}"]
            if sel:
                fields.append(f"window = {cls._lua_str(sel)}")
            if verb == "movetoworkspacesilent":
                fields.append("follow = false")
            return "hl.dsp.window.move({ " + ", ".join(fields) + " })"

        return None

    @classmethod
    def _translate_keyword(cls, name: str, value: str) -> str:
        """Walk a hyprlang-style keyword path (`input:tablet:output`)
        and build the equivalent nested `hl.config` table. Hyphens in
        keys become underscores to match Hyprland's own stub
        generator."""
        parts = [p.replace("-", "_") for p in name.replace(":", ".").split(".")]
        expr = cls._lua_value(value)
        for part in reversed(parts):
            expr = "{ " + part + " = " + expr + " }"
        return "hl.config(" + expr + ")"

    @classmethod
    def _lua_value(cls, v: str) -> str:
        if v.lower() in ("true", "false"):
            return v.lower()
        try:
            int(v)
            return v
        except ValueError:
            pass
        try:
            float(v)
            return v
        except ValueError:
            pass
        return cls._lua_str(v)
