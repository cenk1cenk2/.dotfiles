import json
import logging
import os
import subprocess
import sys
import urllib.error
import urllib.request
from enum import StrEnum
from typing import Any, Optional, Protocol

DEFAULT_MODEL = "gemma4:31b-cloud"

log = logging.getLogger(__name__)

class OutputMode(StrEnum):
    CLIPBOARD = "clipboard"
    TYPE = "type"
    STDOUT = "stdout"

class EnrichProvider(StrEnum):
    HTTP = "http"
    CLAUDE = "claude"
    CODEX = "codex"

class OutputAdapter(Protocol):
    """Sink that writes final text somewhere visible to the user."""

    mode: OutputMode

    def write(self, text: str) -> None:
        """Emit the text. Blocking; raises on failure."""
        ...

class EnrichAdapter(Protocol):
    """AI backend that rewrites a raw text through a system+user prompt."""

    provider: EnrichProvider

    def enrich(self, text: str) -> Optional[str]:
        """Return the cleaned text, or None on failure."""
        ...

class ClipboardOutputAdapter:
    """Copies text to the Wayland clipboard via `wl-copy`."""

    mode = OutputMode.CLIPBOARD

    def write(self, text: str) -> None:
        subprocess.run(["wl-copy"], input=text, text=True, check=False)

class TypeOutputAdapter:
    """Types text into the focused window via `ydotool`."""

    mode = OutputMode.TYPE

    def write(self, text: str) -> None:
        subprocess.run(
            [
                "ydotool",
                "type",
                "--key-delay",
                "10",
                "--key-hold",
                "10",
                "--file",
                "-",
            ],
            input=text,
            text=True,
            check=False,
        )

class StdoutOutputAdapter:
    """Writes text to stdout."""

    mode = OutputMode.STDOUT

    def write(self, text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

class HttpEnrichAdapter:
    """OpenAI-compatible chat-completions endpoint."""

    provider = EnrichProvider.HTTP

    def __init__(
        self,
        system_prompt: str,
        user_prompt_template: str,
        base_url: str,
        model: str,
        api_key: str,
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        thinking: str = "none",
        num_ctx: Optional[int] = None,
        user_agent: str = "common/1.0",
    ):
        self.system_prompt = system_prompt
        self.user_prompt_template = user_prompt_template
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self.temperature = temperature
        self.top_p = top_p
        self.thinking = thinking
        self.num_ctx = num_ctx
        self.user_agent = user_agent

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

class ClaudeEnrichAdapter:
    """Claude CLI (haiku model)."""

    provider = EnrichProvider.CLAUDE

    def __init__(self, system_prompt: str, user_prompt_template: str):
        self.system_prompt = system_prompt
        self.user_prompt_template = user_prompt_template

    def enrich(self, text: str) -> Optional[str]:
        proc = subprocess.run(
            [
                "claude",
                "-p",
                "--model",
                "haiku",
                "--system-prompt",
                self.system_prompt,
                self.user_prompt_template.format(text=text),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            log.error("claude enrichment failed (exit=%d)", proc.returncode)
            return None

        return proc.stdout.strip()

class CodexEnrichAdapter:
    """Codex CLI in ephemeral mode."""

    provider = EnrichProvider.CODEX

    def __init__(self, system_prompt: str, user_prompt_template: str):
        self.system_prompt = system_prompt
        self.user_prompt_template = user_prompt_template

    def enrich(self, text: str) -> Optional[str]:
        prompt = (
            f"{self.system_prompt}\n\n{self.user_prompt_template.format(text=text)}"
        )
        proc = subprocess.run(
            ["codex", "exec", "-", "--ephemeral", "--skip-git-repo-check"],
            input=prompt,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            log.error("codex enrichment failed (exit=%d)", proc.returncode)
            return None

        return proc.stdout.strip()

def notify(title: str, message: str, icon: str, timeout: Optional[int] = None) -> None:
    """Send a desktop notification via notify-send."""
    cmd = ["notify-send", title, message, "-i", icon]
    if timeout:
        cmd.extend(["-t", str(timeout)])
    subprocess.run(cmd, check=False)

def signal_waybar(module: str) -> None:
    """Poke waybar to re-render the named custom module."""
    subprocess.run(["waybar-signal.sh", module], check=False)

def load_prompt(filename: str, relative_to: str) -> str:
    """Read a text file living next to the caller.

    Pass `relative_to=__file__` from the calling module."""
    path = os.path.join(os.path.dirname(os.path.abspath(relative_to)), filename)
    with open(path) as f:
        return f.read().strip()
