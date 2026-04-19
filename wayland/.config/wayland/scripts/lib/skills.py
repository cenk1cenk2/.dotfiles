"""Skill + reference loader.

Mirrors the `<skills_dir>/<slug>/SKILL.md` layout mcphub-nvim expects:

  skills/
    <slug>/
      SKILL.md          # YAML frontmatter + markdown body
    references/
      <name>.md         # shared reference fragments

Frontmatter fields we care about:
  - name            : override the directory slug (rare)
  - description     : one-line summary rendered in the palette and
                      returned to MCP `resources/list`
  - references      : list of paths (relative to the skill dir)
                      whose contents get inlined on
                      `skill/{name}/references`

Consumed in two places:
  - `lib.mcp_server` exposes every skill + reference as an MCP resource
  - `pilot.py`'s Ctrl+Space palette enumerates the same skills so the
    UI and the agent see identical data

Keep this module stdlib-only so the MCP subprocess doesn't drag any
GTK imports in."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Optional

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str
    path: str
    frontmatter: dict[str, Any] = field(default_factory=dict)

    @property
    def references(self) -> list[str]:
        """Normalised list of paths declared under `references:`. Single
        strings are promoted to a one-element list; non-str entries
        are dropped silently."""
        raw = self.frontmatter.get("references")
        if isinstance(raw, str):
            return [raw]
        if isinstance(raw, list):
            return [r for r in raw if isinstance(r, str)]
        return []


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split `---`-delimited YAML frontmatter from body. Returns
    `(frontmatter_dict, body)`. Absent frontmatter → `({}, text)`.

    The parser only handles the minimal shape mcphub-nvim writes:
    `key: value` scalar lines and
    ```
    key:
      - item
      - item
    ```
    list blocks. Scalars `true`/`false` → bool; surrounding quotes
    stripped. Unknown YAML constructs (nested dicts, flow arrays,
    anchors) aren't supported — SKILL.md files don't use them."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    end_idx: Optional[int] = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    if end_idx is None:
        return {}, text
    fm: dict[str, Any] = {}
    current_key: Optional[str] = None
    for raw in lines[1:end_idx]:
        stripped = raw.lstrip()
        # List item under the current key.
        if stripped.startswith("- ") and current_key is not None:
            value = stripped[2:].strip()
            value = value.strip().strip('"').strip("'")
            bucket = fm.setdefault(current_key, [])
            if isinstance(bucket, list):
                bucket.append(value)
            continue
        if ":" not in raw:
            continue
        key, _, value = raw.partition(":")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        current_key = key
        if value == "":
            # Either a list block starting next line or an empty
            # scalar. Start as empty list; a scalar on the next line
            # would be unusual and we'd just return the empty list.
            fm[key] = []
        else:
            value = value.strip('"').strip("'")
            if value == "true":
                fm[key] = True
            elif value == "false":
                fm[key] = False
            else:
                fm[key] = value
    body = "\n".join(lines[end_idx + 1 :]).strip()
    return fm, body


def parse_skill(path: str, *, fallback_name: Optional[str] = None) -> Optional[Skill]:
    """Read and parse `SKILL.md` at `path`. Returns None on IO errors
    or when the body is empty (matches mcphub-nvim's pruning)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        log.debug("skill read failed %s: %s", path, e)
        return None
    fm, body = _parse_frontmatter(text)
    if not body.strip():
        return None
    slug = fallback_name or os.path.basename(os.path.dirname(path))
    name = fm.get("name") or slug
    description = fm.get("description") or f"Guidance for {name}"
    return Skill(
        name=str(name),
        description=str(description),
        body=body,
        path=path,
        frontmatter=fm,
    )


def load_skills(skills_dir: str) -> list[Skill]:
    """Enumerate every `<skills_dir>/<slug>/SKILL.md` and return the
    parsed skills, sorted by name. Silent on any IO failure — missing
    directory / unreadable subpaths just shrink the list."""
    if not skills_dir:
        return []
    out: list[Skill] = []
    try:
        entries = sorted(os.listdir(skills_dir))
    except OSError as e:
        log.debug("skills-dir enumerate failed %s: %s", skills_dir, e)
        return []
    for entry in entries:
        skill_md = os.path.join(skills_dir, entry, "SKILL.md")
        if not os.path.isfile(skill_md):
            continue
        skill = parse_skill(skill_md, fallback_name=entry)
        if skill is not None:
            out.append(skill)
    return out


def load_references(skills_dir: str) -> list[tuple[str, str]]:
    """Return `(name, path)` for every `<skills_dir>/references/*.md`
    sorted by name."""
    if not skills_dir:
        return []
    refs_dir = os.path.join(skills_dir, "references")
    if not os.path.isdir(refs_dir):
        return []
    out: list[tuple[str, str]] = []
    try:
        entries = sorted(os.listdir(refs_dir))
    except OSError:
        return []
    for entry in entries:
        if not entry.endswith(".md"):
            continue
        path = os.path.join(refs_dir, entry)
        if os.path.isfile(path):
            out.append((entry[:-3], path))
    return out


def read_reference(skills_dir: str, name: str) -> Optional[str]:
    """Return the full contents of a shared reference, or None if the
    name doesn't resolve to a real file under `references/`."""
    if not skills_dir or not name:
        return None
    candidate = os.path.join(skills_dir, "references", f"{name}.md")
    if not os.path.isfile(candidate):
        return None
    try:
        with open(candidate, "r", encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        log.debug("reference read failed %s: %s", candidate, e)
        return None


@dataclass(frozen=True)
class SkillListing:
    """One palette row's worth of info. Deliberately slim — the full
    SKILL.md body is only fetched if/when the agent asks for it via an
    MCP `resources/read`, not while the user scans names."""

    name: str
    description: str
    uri: str


def list_skills_via_mcp(
    mcp_server_script: str,
    *,
    skills_dir: Optional[str] = None,
    timeout: float = 5.0,
) -> list[SkillListing]:
    """Spawn `mcp_server_script` as a subprocess, speak the MCP
    handshake + `resources/list` over stdio, and return the `skill/*`
    entries. Used by pilot's Ctrl+Space palette so UI listing goes
    through the exact same server (and therefore the same parser +
    env) the agent would hit to actually read the content.

    Errors collapse to an empty list — the palette gracefully shows no
    skills rather than crashing on a missing binary / bad env."""
    env = dict(os.environ)
    if skills_dir:
        env["PILOT_SKILLS_DIR"] = skills_dir
    # stderr flows straight through so `logging` output from the MCP
    # server surfaces in pilot's own stderr — debugging why the palette
    # shows no skills used to mean blindly re-running the server by
    # hand; now it lands in the same log stream as pilot's other
    # chatter.
    try:
        proc = subprocess.Popen(
            [sys.executable, "-u", mcp_server_script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,
            text=True,
            bufsize=1,
            env=env,
        )
    except OSError as e:
        log.warning("skills mcp spawn failed: %s", e)
        return []
    # Pin narrowed references so closures don't lose the `is not None`
    # check ty otherwise complains about on `proc.stdin` / `proc.stdout`
    # inside the nested send/recv helpers.
    stdin = proc.stdin
    stdout = proc.stdout
    assert stdin is not None and stdout is not None

    def send(obj: dict) -> None:
        stdin.write(json.dumps(obj) + "\n")
        stdin.flush()

    def recv() -> Optional[dict]:
        line = stdout.readline()
        if not line:
            return None
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            return None

    resources: list[dict] = []
    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        recv()
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "resources/list"})
        resp = recv() or {}
        resources = ((resp.get("result") or {}).get("resources") or [])
    except (BrokenPipeError, OSError) as e:
        log.warning("skills mcp roundtrip failed: %s", e)
    finally:
        try:
            stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()

    out: list[SkillListing] = []
    for r in resources:
        uri = r.get("uri", "")
        # Accept both `skill/<name>` (legacy) and `pilot://skill/<name>`
        # (current) — the MCP server emits the pilot:// form now for
        # compatibility with strict clients, but older caches may still
        # round-trip the unscoped URIs.
        body = uri
        for prefix in ("pilot://", ""):
            if prefix and body.startswith(prefix):
                body = body[len(prefix):]
                break
        if not body.startswith("skill/"):
            continue
        tail = body[len("skill/"):]
        if "/" in tail:
            continue
        out.append(
            SkillListing(
                name=r.get("name") or tail,
                description=r.get("description", ""),
                uri=uri,
            )
        )
    log.info(
        "list_skills_via_mcp: %d resources total, %d matched skill/*",
        len(resources),
        len(out),
    )
    return out


def load_skill_references(skills_dir: str, skill_name: str) -> Optional[str]:
    """Inline every reference declared in `<skill_name>`'s frontmatter.
    Returns a `--- <basename> ---`-separated concatenation, matching
    mcphub-nvim's `skill/{name}/references` handler. Missing files
    surface at the end under a `NOT FOUND` banner. None when the skill
    itself can't be resolved."""
    if not skills_dir or not skill_name:
        return None
    skill_folder = os.path.join(skills_dir, skill_name)
    skill_md = os.path.join(skill_folder, "SKILL.md")
    skill = parse_skill(skill_md, fallback_name=skill_name)
    if skill is None:
        return None
    refs = skill.references
    if not refs:
        return f"No references declared in {skill_name} frontmatter."
    results: list[str] = []
    missing: list[str] = []
    for rel in refs:
        abs_path = os.path.realpath(os.path.join(skill_folder, rel.strip()))
        if os.path.isfile(abs_path):
            try:
                with open(abs_path, "r", encoding="utf-8") as f:
                    content = f.read()
                basename = os.path.basename(abs_path)
                results.append(f"--- {basename} ---\n{content}")
                continue
            except OSError:
                pass
        missing.append(rel)
    if missing:
        results.append(f"\n--- NOT FOUND: {', '.join(missing)} ---")
    return "\n\n".join(results) if results else f"No readable references for {skill_name}."
