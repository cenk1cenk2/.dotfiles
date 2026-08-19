"""AI enrichment backends.

Two backends behind one `enrich(text) -> str | None` call: an
OpenAI-compatible HTTP endpoint, and every agent CLI at once through
hyprpilot. Callers configure both through a single `EnrichSpec` and
instantiate via `build_enricher`.

Picking an agent is picking a hyprpilot profile — model, permission
mode, MCP set and the vendor's config-dir env all live there, so a new
backend is a config entry rather than a class here."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any, Protocol

import click

from .cli import run

class EnrichProvider(StrEnum):
    HTTP = "http"
    HYPRPILOT = "hyprpilot"

DEFAULT_ENRICH_ADAPTER = EnrichProvider.HTTP
# Cheap, fast, and patched to carry no MCP servers and a read-only mode.
DEFAULT_PROFILE = "personal/claude/haiku"
DEFAULT_BASE_URL = "https://ai.kilic.dev/v1"
DEFAULT_API_KEY_ENV = "AI_KILIC_DEV_API_KEY"
DEFAULT_TIMEOUT = 120.0
THINKING_LEVELS = ("high", "medium", "low", "none")

log = logging.getLogger(__name__)

@dataclass
class EnrichSpec:
    """Every knob every backend accepts, in one shape.

    Shared by the click layer and speech's socket payload so both
    configure the adapters identically. The API key travels as the
    *name* of an env var, never the secret — the adapter resolves it at
    call time, which also keeps it out of the IPC JSON."""

    provider: EnrichProvider = DEFAULT_ENRICH_ADAPTER
    timeout: float = DEFAULT_TIMEOUT

    # Whatever names the thing that answers: a model id for http, a hyprpilot
    # profile id for hyprpilot. Each adapter falls back to its own default.
    model: str | None = None

    base_url: str = DEFAULT_BASE_URL
    api_key_env: str = DEFAULT_API_KEY_ENV
    temperature: float | None = None
    top_p: float | None = None
    thinking: str = "none"
    num_ctx: int | None = None
    tool_ids: list[str] | None = None
    files: list[dict[str, Any]] | None = None
    user_agent: str = "enrich/1.0"

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
        raw = kwargs.get("provider")
        if raw is None:
            kwargs.pop("provider", None)
        else:
            try:
                kwargs["provider"] = EnrichProvider(raw)
            except ValueError:
                log.warning("unknown provider %r; falling back to default", raw)
                kwargs.pop("provider")

        return cls(**kwargs)

class EnrichAdapter(Protocol):
    """AI backend that rewrites a raw text through a system+user prompt."""

    provider: EnrichProvider

    def enrich(self, text: str) -> str | None:
        """Return the cleaned text, or None on failure."""
        ...

class EnrichAdapterHttp:
    """OpenAI-compatible chat-completions endpoint."""

    provider = EnrichProvider.HTTP
    DEFAULT_MODEL = "kilic.dev/cheap"

    def __init__(self, system_prompt: str, user_prompt_template: str, spec: EnrichSpec):
        self.system_prompt = system_prompt
        self.user_prompt_template = user_prompt_template
        self.spec = spec
        self.model = spec.model or self.DEFAULT_MODEL

    def enrich(self, text: str) -> str | None:
        spec = self.spec
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {
                    "role": "user",
                    "content": self.user_prompt_template.format(text=text),
                },
            ],
        }
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

class EnrichAdapterHyprpilot:
    """Any agent CLI, addressed by hyprpilot profile.

    `spec.model` is a profile id here. The profile carries the real model,
    permission mode, MCP set and the vendor's config-dir env, so switching or
    adding a backend is a hyprpilot config change and never a code change
    here. Prompts go in through `--file`: clipboard and transcript text is
    unbounded, argv is not."""

    provider = EnrichProvider.HYPRPILOT
    DEFAULT_MODEL = DEFAULT_PROFILE

    def __init__(self, system_prompt: str, user_prompt_template: str, spec: EnrichSpec):
        self.system_prompt = system_prompt
        self.user_prompt_template = user_prompt_template
        self.spec = spec
        self.profile = spec.model or self.DEFAULT_MODEL

    def enrich(self, text: str) -> str | None:
        prompt = (
            f"{self.system_prompt}\n\n{self.user_prompt_template.format(text=text)}"
        )
        with tempfile.TemporaryDirectory(prefix="enrich-") as tmp:
            prompt_path = os.path.join(tmp, "prompt.txt")
            with open(prompt_path, "w") as fh:
                fh.write(prompt)

            argv = ["hyprpilot", self.profile, "--file", prompt_path]
            log.info("hyprpilot enrichment: profile=%s", self.profile)
            try:
                result = run(argv, log=log, tag="hyprpilot", timeout=self.spec.timeout)
            except subprocess.TimeoutExpired:
                log.error(
                    "hyprpilot enrichment timed out after %.0fs", self.spec.timeout
                )
                return None

        if result.returncode != 0 or not result.stdout.strip():
            log.error(
                "hyprpilot enrichment failed (exit=%d) stderr=%s",
                result.returncode,
                result.stderr.strip(),
            )
            return None
        return result.stdout.strip()

def build_enricher(
    spec: EnrichSpec, system_prompt: str, user_prompt_template: str
) -> EnrichAdapter:
    """Instantiate the adapter `spec.provider` names."""
    match spec.provider:
        case EnrichProvider.HTTP:
            return EnrichAdapterHttp(system_prompt, user_prompt_template, spec)
        case EnrichProvider.HYPRPILOT:
            return EnrichAdapterHyprpilot(system_prompt, user_prompt_template, spec)
        case _:
            raise ValueError(f"unknown enrich provider: {spec.provider!r}")

def enrich_options(prefix: str = ""):
    """Stack the backend knobs every enriching command shares.

    `prefix` namespaces the flags and the callback kwargs together, so a
    script that only enriches takes `--model` while one where enrichment
    is a single step takes `--enrich-model` and keeps `--model` for its
    own use. Pair with `spec_from_options`, which reads the same prefix."""
    head = f"--{prefix}-" if prefix else "--"
    options = [
        click.option(
            f"{head}provider",
            type=click.Choice(
                [p.value for p in EnrichProvider], case_sensitive=False
            ),
            default=DEFAULT_ENRICH_ADAPTER.value,
            help="Enrichment backend.",
        ),
        click.option(
            f"{head}base-url", default=DEFAULT_BASE_URL, help="HTTP backend base URL."
        ),
        click.option(
            f"{head}api-key-env",
            default=DEFAULT_API_KEY_ENV,
            help="Env var holding the HTTP backend key.",
        ),
        click.option(
            f"{head}model", default=None, help="Model id, or hyprpilot profile id."
        ),
        click.option(f"{head}temperature", type=float, default=None),
        click.option(f"{head}top-p", type=float, default=None),
        click.option(
            f"{head}thinking",
            type=click.Choice(THINKING_LEVELS),
            default="none",
            help="Reasoning depth.",
        ),
        click.option(f"{head}num-ctx", type=int, default=None),
        click.option(
            f"{head}timeout",
            type=float,
            default=DEFAULT_TIMEOUT,
            help="Backend deadline in seconds.",
        ),
    ]

    def decorate(f):
        for option in reversed(options):
            f = option(f)
        return f

    return decorate

def spec_from_options(
    options: dict[str, Any], user_agent: str, prefix: str = ""
) -> EnrichSpec:
    """Fill a spec from the kwargs `enrich_options` injected."""
    head = f"{prefix}_" if prefix else ""

    def value(name: str) -> Any:
        return options[f"{head}{name}"]

    return EnrichSpec(
        provider=EnrichProvider(value("provider")),
        model=value("model"),
        timeout=value("timeout"),
        base_url=value("base_url"),
        api_key_env=value("api_key_env"),
        temperature=value("temperature"),
        top_p=value("top_p"),
        thinking=value("thinking"),
        num_ctx=value("num_ctx"),
        user_agent=user_agent,
    )
