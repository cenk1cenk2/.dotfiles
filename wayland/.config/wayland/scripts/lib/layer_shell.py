"""`libgtk4-layer-shell.so.0` LD_PRELOAD re-exec helper.

Split out into its OWN module so callers can import `ensure_layer_shell_preload`
WITHOUT dragging `gi` / `Gtk` / `Gdk` / `Gtk4LayerShell` in along for the
ride. Importing gi BEFORE the re-exec has set LD_PRELOAD means the shim
that gtk4-layer-shell relies on to hook libwayland never attaches, and
`Gtk4LayerShell.is_supported()` returns False — the overlay then renders
as a normal xdg_toplevel instead of an anchored layer surface.

Keep this module import-cheap: stdlib only, no gi, no overlay imports.
"""

from __future__ import annotations

import os
import sys
from typing import Optional

_LAYER_SHELL_SONAME = "libgtk4-layer-shell.so.0"


def ensure_layer_shell_preload(script_path: Optional[str] = None) -> None:
    """Re-exec the current Python process with `libgtk4-layer-shell.so.0`
    on LD_PRELOAD if it isn't already loaded. No-op when the preload is
    in place. Pass `script_path` explicitly when the module's __file__
    isn't the real entry point (e.g. a wrapper that invokes a bundled
    script). Defaults to sys.argv[0] so a direct `python pilot.py ...`
    invocation works out of the box."""
    current = os.environ.get("LD_PRELOAD", "")
    if _LAYER_SHELL_SONAME in current.split(":"):
        return
    env = os.environ.copy()
    env["LD_PRELOAD"] = (
        f"{current}:{_LAYER_SHELL_SONAME}" if current else _LAYER_SHELL_SONAME
    )
    script = script_path or sys.argv[0]
    os.execvpe(sys.executable, [sys.executable, script, *sys.argv[1:]], env)


# Back-compat alias for call-sites that predated the rename.
_ensure_layer_shell_preload = ensure_layer_shell_preload
