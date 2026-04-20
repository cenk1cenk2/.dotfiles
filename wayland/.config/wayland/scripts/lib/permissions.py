"""Per-session tool-permission state.

Three disjoint sets, all keyed by a normalised tool name:

  - `trusted`         — future permission prompts short-circuit to `allow_once`.
  - `auto_approved`   — same outcome, but rendered as a separate pill so the
                        user can distinguish session-local "trust" (built up
                        by clicking ✓ trust) from command-line seeded allow
                        lists (`--auto-approve`). Authorial intent only.
  - `auto_rejected`   — prompts short-circuit to `reject_always`.

`decide(name)` returns one of the ACP PermissionOptionKind literals the UI
buttons already map to, or None when the caller should ask the user."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Literal, Optional

PermissionKind = Literal["allow_once", "allow_always", "reject_once", "reject_always"]


@dataclass
class PermissionState:
    trusted: set[str] = field(default_factory=set)
    auto_approved: set[str] = field(default_factory=set)
    auto_rejected: set[str] = field(default_factory=set)

    @staticmethod
    def normalise_tool_name(name: str) -> str:
        """Canonicalise a tool name for set membership: lowercase,
        `-` → `_`. So `Read`, `read`, `read-file`, `read_file` all
        land on the same key."""
        return (name or "").lower().replace("-", "_")

    @classmethod
    def from_seeds(
        cls,
        *,
        auto_approve: Iterable[str] = (),
        auto_reject: Iterable[str] = (),
        trusted: Iterable[str] = (),
    ) -> PermissionState:
        state = cls()
        for n in auto_approve:
            state.auto_approved.add(cls.normalise_tool_name(n))
        for n in auto_reject:
            state.auto_rejected.add(cls.normalise_tool_name(n))
        for n in trusted:
            state.trusted.add(cls.normalise_tool_name(n))
        return state

    def trust(self, name: str) -> None:
        self.trusted.add(self.normalise_tool_name(name))

    def auto_approve(self, name: str) -> None:
        key = self.normalise_tool_name(name)
        self.auto_approved.add(key)
        self.auto_rejected.discard(key)

    def auto_reject(self, name: str) -> None:
        key = self.normalise_tool_name(name)
        self.auto_rejected.add(key)
        self.auto_approved.discard(key)

    def discard(self, name: str) -> None:
        """Drop `name` from every set. Used by compose-bar pill removal."""
        key = self.normalise_tool_name(name)
        self.trusted.discard(key)
        self.auto_approved.discard(key)
        self.auto_rejected.discard(key)

    def decide(self, name: str, kind: str = "") -> Optional[PermissionKind]:
        """Return the auto-resolve kind for `(name, kind)`, or None to
        ask the user. Reject wins over approve.

        Seeds are stored in the canonical `mcp_<server>_<tool>` form
        that the user sees in agent tool listings. Incoming tool
        names come in three conventions:

          - `mcp_<server>_<tool>`  — pilot's own / Claude-collapsed
          - `mcp__<server>__<tool>` — Claude raw
          - `<server>_<tool>`       — opencode / most ACP agents

        We canonicalise each to `mcp_<server>_<tool>` (collapse
        double underscores; prefix `mcp_` if missing) and do an exact
        set membership check. No suffix wildcards.

        `kind` is the ACP `ToolKind` (`read` / `edit` / `execute` / …)
        checked after the name forms — a seed of `edit` will match
        both Claude's `Edit` / `Write` tools AND opencode's `edit`
        permission category, regardless of how each agent spells the
        title on the wire."""
        candidates = self._canonical_forms(name)
        if kind:
            kind_key = self.normalise_tool_name(kind)
            if kind_key and kind_key not in candidates:
                candidates.append(kind_key)
        for candidate in candidates:
            if candidate in self.auto_rejected:
                return "reject_always"
            if candidate in self.auto_approved or candidate in self.trusted:
                return "allow_once"
        return None

    @classmethod
    def _canonical_forms(cls, name: str) -> list[str]:
        """Return every plausible canonical spelling of `name`. Always
        tries the normalised raw name plus an `mcp_`-prefixed variant
        so seeds stored as `mcp_github_search_code` match an incoming
        `github_search_code` from opencode."""
        raw = cls.normalise_tool_name(name)
        collapsed = re.sub(r"_{2,}", "_", raw)
        forms = [collapsed]
        if not collapsed.startswith("mcp_"):
            forms.append(f"mcp_{collapsed}")
        if raw != collapsed:
            forms.append(raw)
        return forms


# Module-level alias so existing `from lib import normalise_tool_name`
# / `from lib.permissions import normalise_tool_name` imports don't
# break. Canonical home is `PermissionState.normalise_tool_name`.
normalise_tool_name = PermissionState.normalise_tool_name
