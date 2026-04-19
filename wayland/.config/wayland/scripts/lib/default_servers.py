"""Default MCP server catalog — ported from the nvim mcphub config.

Mirrors the entries in `~/.config/nvim/utils/mcphub/servers.json` that
are routinely useful to a pilot session. The catalog holds the RAW
spec dict (command / args / env OR url / type / headers) with env-var
placeholders left verbatim as `${VAR}` — `_expand_env()` is applied at
build time (not import time) so variables set after module load still
resolve correctly.

Two exports:

- `DEFAULT_SERVERS`     : `{name: {command, args, env, url, type, headers}}`
                          Every value is safe to JSON-serialise as-is;
                          pass a copy through `_expand_env()` before
                          handing off to `McpConfig.add()`.

- `DEFAULT_SERVER_NAMES`: display-order tuple used by pilot.py's
                          `--all-default-mcp` shortcut. Keeps the
                          ordering stable across runs.

Naming convention: nvim keys the JSON with `/` separators
(`argocd/kilic`, `grafana/laravel`). The catalog here uses `_` because
Claude's MCP name validation is stricter and the pilot overlay's pill
labels read better without slashes. Any future key that needs to be
re-added should pick the same `snake_case` form.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

log = logging.getLogger(__name__)

# Regex for the `${VAR}` placeholders mcphub-nvim writes into its env
# / headers maps. The `$VAR` bareword form isn't used upstream so we
# don't try to parse it here — keeps the substitution unambiguous.
_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_value(value: str) -> str:
    """Replace every `${VAR}` occurrence in `value` with the matching
    `os.environ` entry. Missing vars collapse to the empty string and
    emit a debug-level log — we never crash on a missing credential
    so the subprocess can decide for itself how to fail (or degrade).

    Multiple placeholders in one string are all substituted; text
    outside the placeholder is preserved verbatim so a
    `"Bearer ${NVIM_X}"` header still ends up formatted correctly."""

    def repl(match: "re.Match[str]") -> str:
        var = match.group(1)
        val = os.environ.get(var)
        if val is None:
            log.debug("default_servers: env var %s not set; expanding to ''", var)

            return ""

        return val

    return _ENV_RE.sub(repl, value)


def _expand_env(spec: "dict[str, Any]") -> "dict[str, Any]":
    """Return a shallow copy of `spec` with `env` / `headers` values
    resolved against the current `os.environ`. `command` / `args` /
    `url` pass through untouched because mcphub-nvim never templates
    them. Nested structures inside env/headers (unlikely but defended
    against) are left alone — only string values get expanded."""
    out: "dict[str, Any]" = dict(spec)
    for field in ("env", "headers"):
        sub = spec.get(field)
        if not isinstance(sub, dict):
            continue
        expanded: "dict[str, str]" = {}
        for k, v in sub.items():
            if isinstance(v, str):
                expanded[k] = _expand_value(v)
            else:
                expanded[k] = v  # type: ignore[assignment]
        out[field] = expanded

    return out


# Canonical catalog. Order matches the user's preferred display order
# (exported below as `DEFAULT_SERVER_NAMES`). Env-var placeholders are
# kept verbatim — resolve at build time via `_expand_env()`.
DEFAULT_SERVERS: "dict[str, dict[str, Any]]" = {
    "argocd_kilic": {
        "command": "bunx",
        "args": ["-y", "argocd-mcp@latest", "stdio"],
        "env": {
            "ARGOCD_API_TOKEN": "${NVIM_ARGOCD_KILIC_TOKEN}",
            "ARGOCD_BASE_URL": "${NVIM_ARGOCD_KILIC_URL}",
        },
    },
    "context7": {
        "type": "http",
        "url": "https://mcp.context7.com/mcp",
        "headers": {
            "CONTEXT7_API_KEY": "${NVIM_CONTEXT7}",
        },
    },
    "exa": {
        "type": "http",
        "url": "https://mcp.exa.ai/mcp",
    },
    "excalidraw": {
        "type": "http",
        "url": "https://mcp.excalidraw.com",
    },
    "git": {
        "command": "uvx",
        "args": ["mcp-server-git@latest"],
    },
    "github": {
        "type": "http",
        "url": "https://api.githubcopilot.com/mcp/",
        "headers": {
            "Authorization": "Bearer ${NVIM_GITHUB}",
            "X-MCP-Insiders": "true",
        },
    },
    "gitlab": {
        "command": "npx",
        "args": ["-y", "@zereight/mcp-gitlab"],
        "env": {
            "GITLAB_API_URL": "https://gitlab.kilic.dev/api/v4/",
            "GITLAB_PERSONAL_ACCESS_TOKEN": "${NVIM_GITLAB}",
            "GITLAB_READ_ONLY_MODE": "true",
            "USE_PIPELINE": "true",
        },
    },
    "grafana_kilic": {
        "command": "uvx",
        "args": ["mcp-grafana"],
        "env": {
            "GRAFANA_SERVICE_ACCOUNT_TOKEN": "${NVIM_GRAFANA_KILIC_TOKEN}",
            "GRAFANA_URL": "${NVIM_GRAFANA_KILIC_URL}",
        },
    },
    "grafana_laravel": {
        "command": "uvx",
        "args": ["mcp-grafana"],
        "env": {
            "GRAFANA_SERVICE_ACCOUNT_TOKEN": "${NVIM_GRAFANA_LARAVEL_TOKEN}",
            "GRAFANA_URL": "${NVIM_GRAFANA_LARAVEL_URL}",
        },
    },
    "linear_kilic": {
        "type": "sse",
        "url": "https://mcp.linear.app/sse",
        "headers": {
            "Authorization": "Bearer ${NVIM_LINEAR_KILIC}",
        },
    },
    "linear_laravel": {
        "type": "http",
        "url": "https://mcp.linear.app/mcp",
        "headers": {
            "Authorization": "Bearer ${NVIM_LINEAR_WORK}",
        },
    },
    "obsidian": {
        "command": "bunx",
        "args": ["-y", "obsidian-mcp-server@latest"],
        "env": {
            "OBSIDIAN_API_KEY": "${NVIM_OBSIDIAN}",
            "OBSIDIAN_BASE_URL": "https://127.0.0.1:27124",
            "OBSIDIAN_ENABLE_CACHE": "true",
            "OBSIDIAN_VERIFY_SSL": "false",
        },
    },
    "playwright": {
        "command": "bunx",
        "args": ["@playwright/mcp@latest", "--executable-path", "/usr/bin/brave"],
    },
    "sequentialthinking": {
        "command": "bunx",
        "args": ["-y", "@modelcontextprotocol/server-sequential-thinking@latest"],
    },
    "slack_kilic": {
        "command": "bunx",
        "args": ["-y", "@modelcontextprotocol/server-slack@latest"],
        "env": {
            "SLACK_BOT_TOKEN": "${NVIM_SLACK_TOKEN}",
            "SLACK_TEAM_ID": "${NVIM_SLACK_TEAM_ID}",
        },
    },
    "tavily": {
        "type": "http",
        "url": "https://mcp.tavily.com/mcp",
        "headers": {
            "Authorization": "Bearer ${NVIM_TAVILY_API_KEY}",
        },
    },
    "tmux": {
        "command": "bunx",
        "args": ["-y", "tmux-mcp", "--shell-type=zsh"],
    },
}


# Display / lookup order for `pilot.py --all-default-mcp` and any UI
# that needs a stable sort. Keep in sync with `DEFAULT_SERVERS`.
DEFAULT_SERVER_NAMES: "tuple[str, ...]" = (
    "argocd_kilic",
    "context7",
    "exa",
    "excalidraw",
    "git",
    "github",
    "gitlab",
    "grafana_kilic",
    "grafana_laravel",
    "linear_kilic",
    "linear_laravel",
    "obsidian",
    "playwright",
    "sequentialthinking",
    "slack_kilic",
    "tavily",
    "tmux",
)


def get_server(name: str) -> "dict[str, Any]":
    """Return the env-expanded spec for `name`. Raises `KeyError` with
    a list of known names when the caller asks for something we don't
    have — keeps callsite debugging cheap."""
    try:
        raw = DEFAULT_SERVERS[name]
    except KeyError as e:
        raise KeyError(
            f"unknown default server {name!r}; known: "
            f"{', '.join(DEFAULT_SERVER_NAMES)}"
        ) from e

    return _expand_env(raw)
