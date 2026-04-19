#!/usr/bin/env python3
"""Subprocess stub. Claude spawns us via `--mcp-config` with the path
to this script; we rebuild the same `McpServer` configuration ask.py
advertised and call `.run()` on stdio.

All the real wiring — tool registration, approval callback,
protocol — lives in `lib.mcp`. Keeping this file thin means adding or
swapping tools only touches ask.py + lib/mcp.py."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib.mcp import McpServer, socket_approval  # noqa: E402

SOCKET_PATH = os.path.join(
    os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}",
    "wayland-ask-mcp.sock",
)

def main() -> None:
    server = McpServer("ask")
    server.enable_approval(socket_approval(SOCKET_PATH))
    server.run()

if __name__ == "__main__":
    main()
