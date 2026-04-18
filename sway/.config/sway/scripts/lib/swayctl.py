"""Thin wrapper around `i3ipc.Connection` with the patterns our scripts reuse.

The class exposes:

- `command(cmd)` — send a sway command and assert every reply succeeded;
  print failures to stderr before raising.
- Workspace queries: `active_workspace()`, `workspaces()`,
  `workspace_numbers()`, `first_empty_workspace_number()`,
  `workspaces_on_output(name)`.
"""

import sys
from typing import Any

import i3ipc

class Swayctl:
    def __init__(self, ipc: "i3ipc.Connection | None" = None):
        self._ipc = ipc or i3ipc.Connection()

    def command(self, cmd: str) -> None:
        print(f"send: {cmd}")
        replies = self._ipc.command(cmd)
        for reply in replies:
            if reply.error:
                print(f"fail: {cmd} -> {reply.error}", file=sys.stderr)
            assert reply.success

    def active_workspace(self) -> Any:
        return self._ipc.get_tree().find_focused().workspace()

    def workspaces(self) -> list[Any]:
        return self._ipc.get_tree().workspaces()

    def workspace_numbers(self) -> list[int]:
        return [ws.num for ws in self.workspaces()]

    def first_empty_workspace_number(self) -> int:
        used = set(self.workspace_numbers())

        return min(set(range(1, max(used, default=0) + 2)) - used)

    def workspaces_on_output(self, output_name: str) -> list[Any]:
        return [
            ws for ws in self.workspaces()
            if ws.ipc_data["output"] == output_name
        ]
