"""AI enrichment backends.

Each adapter carries its own system + user prompt templates plus any
transport config; callers pick one based on args."""

import json
import logging
import os
import urllib.error
import urllib.request
from enum import StrEnum
from typing import Any, Optional, Protocol

from .cli import run

class EnrichProvider(StrEnum):
    HTTP = "http"
    CLAUDE = "claude"
    OPENCODE = "opencode"

DEFAULT_ENRICH_ADAPTER = EnrichProvider.HTTP

log = logging.getLogger(__name__)

class EnrichAdapter(Protocol):
    """AI backend that rewrites a raw text through a system+user prompt."""

    provider: EnrichProvider

    def enrich(self, text: str) -> Optional[str]:
        """Return the cleaned text, or None on failure."""
        ...

class EnrichAdapterHttp:
    """OpenAI-compatible chat-completions endpoint.

    Positional args: `system_prompt`, `user_prompt_template`. Everything
    else via `**kwargs`, resolved with `kwargs.get(name) or DEFAULT` so
    None-valued caller args (e.g. argparse) collapse to the baseline."""

    provider = EnrichProvider.HTTP

    def __init__(self, system_prompt: str, user_prompt_template: str, **kwargs):
        self.system_prompt = system_prompt
        self.user_prompt_template = user_prompt_template
        self.base_url = kwargs.get("base_url") or "https://ai.kilic.dev/api/v1"
        self.model = kwargs.get("model") or "gemma4:e2b"
        self.api_key = kwargs.get("api_key") or ""
        self.temperature = kwargs.get("temperature")
        self.top_p = kwargs.get("top_p")
        self.thinking = kwargs.get("thinking") or "none"
        self.num_ctx = kwargs.get("num_ctx")
        self.user_agent = kwargs.get("user_agent") or "enrich/1.0"
        # OpenWebUI extensions: server-side tool UUIDs (and the pseudo-ids
        # "web_search", "memory", "code_interpreter", "image_generation",
        # "voice" for built-ins, plus "server:mcp:<id>" for MCP). `files`
        # attaches [{"type": "file"|"folder"|"collection", "id": "..."}]
        # for RAG context. Both are silently ignored by non-OpenWebUI
        # servers.
        self.tool_ids = kwargs.get("tool_ids")
        self.files = kwargs.get("files")
        # Accepted for API parity with the CLI adapters — no plan/edit
        # distinction exists at the chat-completions layer.
        self.mode = kwargs.get("mode")

    def enrich(self, text: str) -> Optional[str]:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {
                    "role": "user",
                    "content": self.user_prompt_template.format(text=text),
                },
            ],
            "reasoning_effort": self.thinking,
        }
        if self.temperature is not None:
            body["temperature"] = self.temperature
        if self.top_p is not None:
            body["top_p"] = self.top_p
        if self.num_ctx:
            body["options"] = {"num_ctx": self.num_ctx}
        if self.tool_ids:
            body["tool_ids"] = self.tool_ids
        if self.files:
            body["files"] = self.files

        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": self.user_agent,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            log.error("HTTP %d: %s", e.code, e.read().decode(errors="replace"))
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

    def __init__(self, system_prompt: str, user_prompt_template: str, **kwargs):
        self.system_prompt = system_prompt
        self.user_prompt_template = user_prompt_template
        self.model = kwargs.get("model") or "haiku"
        # Maps to `--permission-mode`. Default "plan" keeps enrich
        # read-only — a rewrite task shouldn't be editing files anyway.
        self.mode = kwargs.get("mode")

    def enrich(self, text: str) -> Optional[str]:
        cmd = [
            "claude",
            "-p",
            "--model",
            self.model,
            *(["--permission-mode", self.mode] if self.mode else []),
            "--system-prompt",
            self.system_prompt,
            self.user_prompt_template.format(text=text),
        ]
        log.info("claude enrichment: model=%s mode=%s", self.model, self.mode or "default")
        result = run(cmd, log=log, tag="claude")
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

    One `opencode run` per call, no session state — rewrites are
    stateless by definition. `--format default` plain-text output goes
    straight to stdout. Models are addressed as `<provider>/<model>`;
    default provider `kilic`, default model `gemma4:e2b`."""

    provider = EnrichProvider.OPENCODE

    DEFAULT_MODEL = "gemma4:e2b"
    DEFAULT_PROVIDER = "kilic"
    DEFAULT_CONFIG_PATH = os.path.expanduser(
        "~/.config/nvim/utils/agents/opencode/kilic.json"
    )

    def __init__(self, system_prompt: str, user_prompt_template: str, **kwargs):
        self.system_prompt = system_prompt
        self.user_prompt_template = user_prompt_template
        self.model = kwargs.get("model") or self.DEFAULT_MODEL
        self.mode = kwargs.get("mode")
        self.config_path = kwargs.get("config_path") or self.DEFAULT_CONFIG_PATH
        self.provider_name = kwargs.get("provider_name") or self.DEFAULT_PROVIDER

    def enrich(self, text: str) -> Optional[str]:
        prompt = (
            f"{self.system_prompt}\n\n{self.user_prompt_template.format(text=text)}"
        )
        model_spec = f"{self.provider_name}/{self.model}"
        argv = [
            "opencode",
            "run",
            "--format",
            "default",
            "--model",
            model_spec,
        ]
        if self.mode:
            argv.extend(["--agent", self.mode])
        argv.append(prompt)

        env = os.environ.copy()
        if self.config_path and os.path.exists(self.config_path):
            env["OPENCODE_CONFIG"] = self.config_path

        log.info("opencode enrichment: model=%s agent=%s", model_spec, self.mode or "default")
        result = run(argv, log=log, env=env, tag="opencode")
        if result.returncode != 0 or not result.stdout.strip():
            log.error(
                "opencode enrichment failed (exit=%d) stderr=%s",
                result.returncode,
                result.stderr.strip(),
            )
            return None
        return result.stdout.strip()
