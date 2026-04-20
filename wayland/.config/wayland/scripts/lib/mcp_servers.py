"""External MCP server catalog — reads nvim's mcphub config.

Pure reader for `~/.config/nvim/utils/mcphub/servers.json`. Pilot's
own built-in MCP server (the `system` one) is NOT registered here;
pilot constructs that entry itself so it has full control over its
environment (skills dir, future runtime vars, etc.). This module is
deliberately scoped to the mcphub dependency so we can delete it
whenever we drop that integration.

Each `mcpServers` entry is translated into the spec shape pilot's ACP
adapter expects — `command` / `args` / `env` for stdio, `url` /
`headers` / `type` for remote. nvim-only fields (`autoApprove`,
`disabled_tools`, etc.) are dropped from the transport spec but
preserved as permission seeds via `get_permission_seeds`.

Names are rewritten `/` → `_` so `argocd/kilic` becomes
`argocd_kilic` — matches stricter ACP-client name validation and
reads cleaner as pilot pill labels.

Env-var placeholders (`${VAR}`) are expanded at `get_server()` call
time, not at module load, so variables set after import still
resolve."""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, ClassVar

log = logging.getLogger(__name__)


class McpServerCatalog:
    """Lazy-loaded index of the mcphub `servers.json` config.

    Instance state is just `_servers` + `_permissions` — the module
    exposes a default singleton (`DEFAULT_CATALOG`) built from
    `CATALOG_PATH`, and forwards the three public entry points
    (`get_server`, `get_permission_seeds`, `server_names`) through
    module-level aliases for the existing import surface. Tests that
    want a different catalog instantiate the class directly with
    `path=…`."""

    CATALOG_PATH: ClassVar[str] = os.path.expanduser(
        "~/.config/nvim/utils/mcphub/servers.json"
    )
    _ENV_RE: ClassVar[re.Pattern[str]] = re.compile(
        r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}"
    )
    _ACP_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {"command", "args", "env", "url", "headers", "type"}
    )

    def __init__(self, path: str | None = None):
        self._path: str = path if path is not None else self.CATALOG_PATH
        self._servers: dict[str, dict[str, Any]]
        self._permissions: dict[str, tuple[list[str], list[str]]]
        self._servers, self._permissions = self._load(self._path)

    # ── Loader internals ────────────────────────────────────────────

    @classmethod
    def _expand_value(cls, value: str) -> str:
        """Replace every `${VAR}` with `os.environ[VAR]` (empty if
        missing)."""

        def repl(match: re.Match[str]) -> str:
            var = match.group(1)
            val = os.environ.get(var)
            if val is None:
                log.debug("mcp_servers: env var %s unset", var)
                return ""
            return val

        return cls._ENV_RE.sub(repl, value)

    @classmethod
    def _expand_env(cls, spec: dict[str, Any]) -> dict[str, Any]:
        """Return a shallow copy of `spec` with `env` / `headers`
        values resolved. Non-string sub-values pass through."""
        out = dict(spec)
        for field in ("env", "headers"):
            sub = spec.get(field)
            if isinstance(sub, dict):
                out[field] = {
                    k: cls._expand_value(v) if isinstance(v, str) else v
                    for k, v in sub.items()
                }
        return out

    @classmethod
    def _translate(
        cls, raw_name: str, spec: dict[str, Any]
    ) -> tuple[str, dict[str, Any], list[str], list[str]] | None:
        """Trim the nvim-only fields and rename `/` + `-` → `_`.
        Returns `(name, transport_spec, auto_approve, auto_reject)`
        or None when the entry is disabled or lacks both stdio and
        remote transport.

        `auto_approve` comes from mcphub's `autoApprove` list;
        `auto_reject` from `disabled_tools`. Both pass through as raw
        tool-name strings so the caller decides whether to normalise
        them."""
        if spec.get("disabled"):
            return None
        clean = {k: v for k, v in spec.items() if k in cls._ACP_FIELDS}
        if not clean.get("command") and not clean.get("url"):
            return None
        url = clean.get("url")
        if (
            "type" not in clean
            and isinstance(url, str)
            and url.rstrip("/").endswith("/sse")
        ):
            clean["type"] = "sse"
        name = raw_name.replace("/", "_").replace("-", "_")
        auto_approve = [
            t for t in (spec.get("autoApprove") or []) if isinstance(t, str)
        ]
        auto_reject = [
            t for t in (spec.get("disabled_tools") or []) if isinstance(t, str)
        ]
        return name, clean, auto_approve, auto_reject

    @classmethod
    def _load(
        cls, path: str
    ) -> tuple[
        dict[str, dict[str, Any]],
        dict[str, tuple[list[str], list[str]]],
    ]:
        """Return `(servers, permissions)` where `permissions[name] =
        (auto_approve, auto_reject)`. Servers dict is the transport
        spec; permissions dict is the per-server seed lists mcphub
        flagged."""
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            log.warning("mcp_servers: catalog not found at %s", path)
            return {}, {}
        except (OSError, json.JSONDecodeError) as e:
            log.warning("mcp_servers: failed to read %s: %s", path, e)
            return {}, {}
        raw = data.get("mcpServers") or {}
        servers: dict[str, dict[str, Any]] = {}
        perms: dict[str, tuple[list[str], list[str]]] = {}
        for raw_name, spec in raw.items():
            if not isinstance(spec, dict):
                continue
            translated = cls._translate(raw_name, spec)
            if translated is None:
                continue
            name, clean, auto_approve, auto_reject = translated
            servers[name] = clean
            if auto_approve or auto_reject:
                perms[name] = (auto_approve, auto_reject)
        return servers, perms

    # ── Public surface ──────────────────────────────────────────────

    @property
    def servers(self) -> dict[str, dict[str, Any]]:
        """Raw (unexpanded) transport specs, keyed by normalised
        name. Kept as a property so mutation goes through
        `__init__` / `reload` rather than ad-hoc writes."""
        return self._servers

    @property
    def server_names(self) -> tuple[str, ...]:
        """Ordered tuple of every catalogued server name."""
        return tuple(self._servers)

    def get_server(self, name: str) -> dict[str, Any]:
        """Return the env-expanded spec for `name`. Accepts both the
        normalised catalog form (`argocd_kilic`) and the raw mcphub
        key (`argocd/kilic`, `linear/kilic-dev`); slashes and hyphens
        are collapsed to `_` on lookup. Raises `KeyError` with a list
        of known names when the caller asks for something we don't
        have."""
        candidate = self._servers.get(name)
        if candidate is None:
            alias = name.replace("/", "_").replace("-", "_")
            candidate = self._servers.get(alias)
        if candidate is None:
            known = ", ".join(self.server_names) or "<empty catalog>"
            raise KeyError(f"unknown default server {name!r}; known: {known}")
        return self._expand_env(candidate)

    def get_permission_seeds(
        self, names: list[str]
    ) -> tuple[list[str], list[str]]:
        """Aggregate per-server `autoApprove` / `disabled_tools` lists
        for every server in `names` into flat `(auto_approve,
        auto_reject)` lists. Each entry is prefixed
        `mcp_<server>_<tool>` so the stored name matches the canonical
        format the user sees in agent tool listings. Duplicates
        de-duped; unknown server names log and skip."""
        approve: list[str] = []
        reject: list[str] = []
        seen_approve: set[str] = set()
        seen_reject: set[str] = set()
        for name in names:
            seeds = self._permissions.get(name)
            if seeds is None:
                continue
            a_list, r_list = seeds
            prefix = f"mcp_{name}_"
            for t in a_list:
                full = f"{prefix}{t}"
                if full not in seen_approve:
                    seen_approve.add(full)
                    approve.append(full)
            for t in r_list:
                full = f"{prefix}{t}"
                if full not in seen_reject:
                    seen_reject.add(full)
                    reject.append(full)
        return approve, reject


# ── Module-level default singleton ─────────────────────────────────
#
# Loaded once at import time from `CATALOG_PATH`. Callers that need a
# different catalog (tests, future alternate-source readers) build
# their own `McpServerCatalog(path=…)` instead of poking these.

DEFAULT_CATALOG = McpServerCatalog()
DEFAULT_SERVERS: dict[str, dict[str, Any]] = DEFAULT_CATALOG.servers
DEFAULT_SERVER_NAMES: tuple[str, ...] = DEFAULT_CATALOG.server_names


def get_server(name: str) -> dict[str, Any]:
    """Back-compat wrapper around `DEFAULT_CATALOG.get_server`."""
    return DEFAULT_CATALOG.get_server(name)


def get_permission_seeds(names: list[str]) -> tuple[list[str], list[str]]:
    """Back-compat wrapper around
    `DEFAULT_CATALOG.get_permission_seeds`."""
    return DEFAULT_CATALOG.get_permission_seeds(names)
