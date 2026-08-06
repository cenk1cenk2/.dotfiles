"""AI enrichment backends.

Four interchangeable backends behind one `enrich(text) -> str | None`
call: an OpenAI-compatible HTTP endpoint and the claude / opencode /
codex CLIs. Callers configure every one of them through a single
`EnrichSpec` and instantiate via `build_enricher` — the per-backend
flag vocabulary stays inside the adapters."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any, ClassVar, Optional, Protocol

from .cli import run

class EnrichProvider(StrEnum):
    HTTP = "http"
    CLAUDE = "claude"
    OPENCODE = "opencode"
    CODEX = "codex"

class EnrichMode(StrEnum):
    """Capability ceiling, normalised across backends.

    Every CLI spells this differently — claude `--permission-mode`,
    opencode `--agent`, codex `--sandbox` — and the values are not
    interchangeable, so callers speak this enum and each adapter
    translates through its own table."""

    READ_ONLY = "read-only"
    EDIT = "edit"
    UNSAFE = "unsafe"

DEFAULT_ENRICH_ADAPTER = EnrichProvider.HTTP
# A rewrite has no business touching the filesystem; opting up is explicit.
DEFAULT_ENRICH_MODE = EnrichMode.READ_ONLY
# OpenWebUI's own `/api/v1` answers `{"detail":"Model not found"}` for every
# model it lists; the ollama passthrough completes against the same ids. The
# OpenWebUI-only extensions below (tool_ids, files) are inert on this route.
DEFAULT_BASE_URL = "https://ai.kilic.dev/ollama/v1"
DEFAULT_API_KEY_ENV = "AI_KILIC_DEV_API_KEY"
DEFAULT_TIMEOUT = 120.0

log = logging.getLogger(__name__)

@dataclass
class EnrichSpec:
    """Every knob every backend accepts, in one shape.

    Shared by the click layer and speech's socket payload so both
    configure the adapters identically. The API key travels as the
    *name* of an env var, never the secret — the adapter resolves it at
    call time, which also keeps it out of the IPC JSON."""

    provider: EnrichProvider = DEFAULT_ENRICH_ADAPTER
    model: Optional[str] = None
    mode: EnrichMode = DEFAULT_ENRICH_MODE
    timeout: float = DEFAULT_TIMEOUT

    base_url: str = DEFAULT_BASE_URL
    api_key_env: str = DEFAULT_API_KEY_ENV
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    thinking: str = "none"
    num_ctx: Optional[int] = None
    tool_ids: Optional[list[str]] = None
    files: Optional[list[dict[str, Any]]] = None
    user_agent: str = "enrich/1.0"

    claude_config_dir: Optional[str] = None

    opencode_provider: Optional[str] = None
    opencode_config_dir: Optional[str] = None
    opencode_db: Optional[str] = None

    codex_home: Optional[str] = None
    codex_reasoning_effort: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> EnrichSpec:
        """Rebuild from a socket payload, dropping keys we no longer know.

        A speech daemon can outlive the CLI that spawned it across an
        edit to this file, so skew degrades to the defaults instead of
        raising. Unknown *values* matter more than unknown keys here —
        adding a provider or mode is exactly the edit that skews — and a
        raise would surface as a dropped socket reply, which the client
        reads as "no session running" and answers by starting a second
        one over the live recorder."""
        kwargs = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        for field, enum in (("provider", EnrichProvider), ("mode", EnrichMode)):
            raw = kwargs.get(field)
            if raw is None:
                kwargs.pop(field, None)
                continue
            try:
                kwargs[field] = enum(raw)
            except ValueError:
                log.warning("unknown %s %r; falling back to default", field, raw)
                kwargs.pop(field)

        return cls(**kwargs)

class EnrichAdapter(Protocol):
    """AI backend that rewrites a raw text through a system+user prompt."""

    provider: EnrichProvider

    def enrich(self, text: str) -> Optional[str]:
        """Return the cleaned text, or None on failure."""
        ...

class EnrichAdapterHttp:
    """OpenAI-compatible chat-completions endpoint."""

    provider = EnrichProvider.HTTP
    DEFAULT_MODEL = "gemma4:31b-cloud"

    def __init__(
        self, system_prompt: str, user_prompt_template: str, spec: EnrichSpec
    ):
        self.system_prompt = system_prompt
        self.user_prompt_template = user_prompt_template
        self.spec = spec
        self.model = spec.model or self.DEFAULT_MODEL

    def enrich(self, text: str) -> Optional[str]:
        spec = self.spec
        # OpenWebUI ≥0.9.5 crashes (`process_chat:2013`,
        # `metadata['chat_id'].startswith('local:')` on None) when an
        # external client omits chat_id — the masked 400 reads
        # `{"detail":"'NoneType' object has no attribute 'startswith'"}`.
        # Just `chat_id` is enough; sending session_id/id/parent_id too
        # makes the server route this as a UI background task and return
        # `{status, task_ids, chat_id}` instead of OpenAI choices.
        # Ref: open-webui/open-webui#24550, #24575.
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {
                    "role": "user",
                    "content": self.user_prompt_template.format(text=text),
                },
            ],
            "chat_id": f"speech-{uuid.uuid4()}",
        }
        # OpenWebUI's reasoning router only accepts high/medium/low; sending
        # "none" makes its task-model lookup return None and the middleware
        # then `.startswith()`s that None.
        if spec.thinking in ("high", "medium", "low"):
            body["reasoning_effort"] = spec.thinking
        if spec.temperature is not None:
            body["temperature"] = spec.temperature
        if spec.top_p is not None:
            body["top_p"] = spec.top_p
        if spec.num_ctx:
            body["options"] = {"num_ctx": spec.num_ctx}
        # OpenWebUI extensions: server-side tool UUIDs (and the pseudo-ids
        # "web_search", "memory", "code_interpreter", "image_generation",
        # "voice" for built-ins, plus "server:mcp:<id>" for MCP). `files`
        # attaches [{"type": "file"|"folder"|"collection", "id": "..."}]
        # for RAG context. Both are silently ignored by non-OpenWebUI
        # servers.
        if spec.tool_ids:
            body["tool_ids"] = spec.tool_ids
        if spec.files:
            body["files"] = spec.files

        payload = json.dumps(body)
        log.debug("request: %s", payload)
        req = urllib.request.Request(
            f"{spec.base_url}/chat/completions",
            data=payload.encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {os.environ.get(spec.api_key_env, '')}",
                "User-Agent": spec.user_agent,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=spec.timeout) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            log.error(
                "HTTP %d (model=%s): %s",
                e.code,
                self.model,
                e.read().decode(errors="replace"),
            )
            return None
        except Exception as e:
            log.error("http completion failed: %s", e)
            return None

        if not data or "choices" not in data or not data["choices"]:
            log.error("unexpected API response: %s", data)
            return None

        result = data["choices"][0]["message"]["content"]
        log.info("enrichment complete (%d chars)", len(result))

        return result

class EnrichAdapterClaude:
    """Claude CLI wrapper. Defaults to haiku for fast one-shot rewrites."""

    provider = EnrichProvider.CLAUDE
    DEFAULT_MODEL = "haiku"
    # Enrichment is a personal-context tool; without this it would inherit
    # whatever CLAUDE_CONFIG_DIR the launching session happened to carry and
    # a dictation rewrite fired from a work profile would land in the work
    # config's history.
    DEFAULT_CONFIG_DIR = os.path.expanduser("~/.claude-kilic")
    PERMISSION_MODES: ClassVar[dict[EnrichMode, str]] = {
        EnrichMode.READ_ONLY: "plan",
        EnrichMode.EDIT: "acceptEdits",
        EnrichMode.UNSAFE: "bypassPermissions",
    }

    def __init__(
        self, system_prompt: str, user_prompt_template: str, spec: EnrichSpec
    ):
        self.system_prompt = system_prompt
        self.user_prompt_template = user_prompt_template
        self.spec = spec
        self.model = spec.model or self.DEFAULT_MODEL
        self.config_dir = spec.claude_config_dir or self.DEFAULT_CONFIG_DIR

    def enrich(self, text: str) -> Optional[str]:
        permission_mode = self.PERMISSION_MODES[self.spec.mode]
        cmd = [
            "claude",
            "-p",
            "--model",
            self.model,
            # No --mcp-config alongside it, so this loads no MCP servers at
            # all. A rewrite needs none, and the startup saving is most of
            # the wall clock.
            "--strict-mcp-config",
            "--permission-mode",
            permission_mode,
            "--system-prompt",
            self.system_prompt,
            self.user_prompt_template.format(text=text),
        ]
        env = os.environ.copy()
        env["CLAUDE_CONFIG_DIR"] = self.config_dir

        log.info(
            "claude enrichment: model=%s permission-mode=%s",
            self.model,
            permission_mode,
        )
        try:
            result = run(
                cmd, log=log, env=env, tag="claude", timeout=self.spec.timeout
            )
        except subprocess.TimeoutExpired:
            log.error("claude enrichment timed out after %.0fs", self.spec.timeout)
            return None
        if result.returncode != 0 or not result.stdout.strip():
            log.error(
                "claude enrichment failed (exit=%d) stderr=%s",
                result.returncode,
                result.stderr.strip(),
            )
            return None
        return result.stdout.strip()

class EnrichAdapterOpenCode:
    """OpenCode CLI in one-shot mode.

    One `opencode run` per call. It has no ephemeral flag — every call
    persists a session row — so the db is pinned explicitly rather than
    left to seed opencode's XDG default. Models are addressed as
    `<provider>/<model>`."""

    provider = EnrichProvider.OPENCODE
    DEFAULT_MODEL = "gemma4:31b-cloud"
    DEFAULT_PROVIDER = "kilic"
    DEFAULT_CONFIG_DIR = os.path.expanduser("~/.config/opencode-kilic")
    DEFAULT_DB = os.path.expanduser("~/.local/share/opencode-kilic/opencode.db")
    AGENTS: ClassVar[dict[EnrichMode, str]] = {
        EnrichMode.READ_ONLY: "plan",
        EnrichMode.EDIT: "build",
        EnrichMode.UNSAFE: "build",
    }

    def __init__(
        self, system_prompt: str, user_prompt_template: str, spec: EnrichSpec
    ):
        self.system_prompt = system_prompt
        self.user_prompt_template = user_prompt_template
        self.spec = spec
        self.model = spec.model or self.DEFAULT_MODEL
        self.provider_name = spec.opencode_provider or self.DEFAULT_PROVIDER
        self.config_dir = spec.opencode_config_dir or self.DEFAULT_CONFIG_DIR
        self.db_path = spec.opencode_db or self.DEFAULT_DB

    def enrich(self, text: str) -> Optional[str]:
        prompt = (
            f"{self.system_prompt}\n\n{self.user_prompt_template.format(text=text)}"
        )
        model_spec = f"{self.provider_name}/{self.model}"
        agent = self.AGENTS[self.spec.mode]
        argv = [
            "opencode",
            "run",
            "--pure",
            "--format",
            "default",
            "--model",
            model_spec,
            "--agent",
            agent,
        ]
        if self.spec.mode == EnrichMode.UNSAFE:
            argv.append("--auto")
        argv.append(prompt)

        env = os.environ.copy()
        env["OPENCODE_CONFIG_DIR"] = self.config_dir
        env["OPENCODE_DB"] = self.db_path

        log.info("opencode enrichment: model=%s agent=%s", model_spec, agent)
        try:
            result = run(
                argv, log=log, env=env, tag="opencode", timeout=self.spec.timeout
            )
        except subprocess.TimeoutExpired:
            log.error("opencode enrichment timed out after %.0fs", self.spec.timeout)
            return None
        if result.returncode != 0 or not result.stdout.strip():
            log.error(
                "opencode enrichment failed (exit=%d) stderr=%s",
                result.returncode,
                result.stderr.strip(),
            )
            return None
        return result.stdout.strip()

class EnrichAdapterCodex:
    """Codex CLI in exec (non-interactive) mode.

    Codex has no system-prompt flag, so the system prompt is folded into
    the message the way the opencode adapter does. `--ignore-user-config`
    keeps `~/.codex/config.toml` — which turns on workspace-write, web
    search, memories and MCP — out of a plain text rewrite; auth still
    resolves through CODEX_HOME."""

    provider = EnrichProvider.CODEX
    # gpt-5.3-codex-spark would be the cheap counterpart to claude's haiku,
    # but a ChatGPT-account login rejects it at the API: "not supported when
    # using Codex with a ChatGPT account".
    DEFAULT_MODEL = "gpt-5.5"
    SANDBOXES: ClassVar[dict[EnrichMode, str]] = {
        EnrichMode.READ_ONLY: "read-only",
        EnrichMode.EDIT: "workspace-write",
        EnrichMode.UNSAFE: "danger-full-access",
    }

    def __init__(
        self, system_prompt: str, user_prompt_template: str, spec: EnrichSpec
    ):
        self.system_prompt = system_prompt
        self.user_prompt_template = user_prompt_template
        self.spec = spec
        self.model = spec.model or self.DEFAULT_MODEL

    def enrich(self, text: str) -> Optional[str]:
        prompt = (
            f"{self.system_prompt}\n\n{self.user_prompt_template.format(text=text)}"
        )
        sandbox = self.SANDBOXES[self.spec.mode]
        env = os.environ.copy()
        if self.spec.codex_home:
            env["CODEX_HOME"] = self.spec.codex_home

        # `--output-last-message` isolates the final assistant turn; stdout
        # also carries it today but would pick up reasoning summaries the
        # moment a config or default enables them. The path is opened only
        # after codex exits, so a write-then-rename writer reads correctly
        # instead of yielding "" and silently falling back to raw stdout.
        with tempfile.TemporaryDirectory(prefix="enrich-codex-") as tmp:
            last_message = os.path.join(tmp, "last-message.txt")
            argv = [
                "codex",
                "exec",
                "--ignore-user-config",
                "--skip-git-repo-check",
                "--ephemeral",
                "--color",
                "never",
                "--sandbox",
                sandbox,
                "--model",
                self.model,
                "--output-last-message",
                last_message,
            ]
            if self.spec.codex_reasoning_effort:
                argv.extend(
                    ["-c", f"model_reasoning_effort={self.spec.codex_reasoning_effort}"]
                )
            argv.append(prompt)

            log.info(
                "codex enrichment: model=%s sandbox=%s", self.model, sandbox
            )
            try:
                result = run(
                    argv, log=log, env=env, tag="codex", timeout=self.spec.timeout
                )
            except subprocess.TimeoutExpired:
                log.error("codex enrichment timed out after %.0fs", self.spec.timeout)
                return None
            if result.returncode != 0:
                log.error(
                    "codex enrichment failed (exit=%d) stderr=%s",
                    result.returncode,
                    result.stderr.strip(),
                )
                return None
            try:
                with open(last_message) as fh:
                    output = fh.read().strip()
            except OSError:
                output = ""
            output = output or result.stdout.strip()

        if not output:
            log.error("codex enrichment produced no output")
            return None
        return output

def build_enricher(
    spec: EnrichSpec, system_prompt: str, user_prompt_template: str
) -> EnrichAdapter:
    """Instantiate the adapter `spec.provider` names."""
    match spec.provider:
        case EnrichProvider.HTTP:
            return EnrichAdapterHttp(system_prompt, user_prompt_template, spec)
        case EnrichProvider.CLAUDE:
            return EnrichAdapterClaude(system_prompt, user_prompt_template, spec)
        case EnrichProvider.OPENCODE:
            return EnrichAdapterOpenCode(system_prompt, user_prompt_template, spec)
        case EnrichProvider.CODEX:
            return EnrichAdapterCodex(system_prompt, user_prompt_template, spec)
        case _:
            raise ValueError(f"unknown enrich provider: {spec.provider!r}")
