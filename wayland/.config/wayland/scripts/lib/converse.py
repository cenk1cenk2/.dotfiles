"""Streaming conversational AI backends.

Sibling of `EnrichAdapter`: where enrichment is a one-shot text rewrite,
these adapters hold a multi-turn session and yield response chunks as they
arrive. `ask.py` drives them from a socket/compose loop."""

import json
import logging
import os
import subprocess
import threading
from typing import Any, Iterator, Optional, Protocol

import requests

from .enrich import EnrichProvider

log = logging.getLogger(__name__)

_STDERR_DRAIN_CAP = 16384

def _spawn_stderr_drain(proc: subprocess.Popen) -> tuple[threading.Thread, list[str]]:
    """Consume stderr in a background thread so the child never blocks on a
    full pipe while we iterate stdout. Keeps at most `_STDERR_DRAIN_CAP` bytes
    for later inclusion in error messages."""
    buf: list[str] = []
    stream = proc.stderr
    if stream is None:
        dummy = threading.Thread(target=lambda: None, daemon=True)
        dummy.start()

        return dummy, buf

    def drain():
        try:
            for line in stream:
                if sum(len(x) for x in buf) < _STDERR_DRAIN_CAP:
                    buf.append(line)
        except Exception:
            pass

    t = threading.Thread(target=drain, daemon=True)
    t.start()

    return t, buf

def _close_pipes(proc: subprocess.Popen) -> None:
    for pipe in (proc.stdout, proc.stderr, proc.stdin):
        if pipe is not None:
            try:
                pipe.close()
            except OSError:
                pass

class ConversationAdapter(Protocol):
    """Streaming, stateful AI backend. Each `turn()` extends the session."""

    provider: EnrichProvider

    def turn(self, user_message: str) -> Iterator[str]:
        """Yield assistant response chunks. Appends this turn to internal
        session state."""
        ...

    def close(self) -> None:
        """Release any subprocess / session handles."""
        ...

class ConversationAdapterHttp:
    """OpenAI-compatible `/chat/completions` with `stream=true`."""

    provider = EnrichProvider.HTTP

    def __init__(
        self,
        system_prompt: str,
        base_url: str,
        model: str,
        api_key: str,
        user_agent: str = "ask/1.0",
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        thinking: str = "none",
        num_ctx: Optional[int] = None,
        tool_ids: Optional[list[str]] = None,
        features: Optional[dict[str, bool]] = None,
        files: Optional[list[dict]] = None,
    ):
        self.system_prompt = system_prompt
        self.base_url = base_url
        self.model = model
        self.api_key = api_key
        self.user_agent = user_agent
        self.temperature = temperature
        self.top_p = top_p
        self.thinking = thinking
        self.num_ctx = num_ctx
        # OpenWebUI extensions (silently ignored by generic OpenAI servers).
        # tool_ids: server-side tool UUIDs (or "server:mcp:<id>").
        # features: toggles for web_search / code_interpreter / image_generation / memory / voice.
        # files: [{"type": "file"|"folder"|"collection", "id": "..."}] for RAG context.
        self.tool_ids = tool_ids
        self.features = features
        self.files = files
        self.messages: list[dict] = [
            {"role": "system", "content": system_prompt},
        ]

    def turn(self, user_message: str) -> Iterator[str]:
        self.messages.append({"role": "user", "content": user_message})
        body: dict[str, Any] = {
            "model": self.model,
            "messages": self.messages,
            "stream": True,
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
        if self.features:
            body["features"] = self.features
        if self.files:
            body["files"] = self.files

        try:
            resp = requests.post(
                f"{self.base_url}/chat/completions",
                json=body,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "User-Agent": self.user_agent,
                    "Accept": "text/event-stream",
                },
                stream=True,
                timeout=(10, 300),
            )
        except requests.RequestException as e:
            log.error("http stream request failed: %s", e)
            raise RuntimeError(f"http request failed: {e}") from e

        if resp.status_code >= 400:
            detail = resp.text[:500]
            log.error("http %d: %s", resp.status_code, detail)
            raise RuntimeError(f"http {resp.status_code}: {detail}")

        collected: list[str] = []
        stream_ok = False
        try:
            for line in resp.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    event = json.loads(payload)
                except json.JSONDecodeError:
                    log.warning("skipping malformed SSE payload: %r", payload[:120])
                    continue
                choices = event.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                chunk = delta.get("content")
                if chunk:
                    collected.append(chunk)
                    yield chunk
            stream_ok = True
        finally:
            resp.close()
            if stream_ok:
                self.messages.append(
                    {"role": "assistant", "content": "".join(collected)},
                )
            else:
                # Roll back the user turn so history stays consistent: a
                # failed stream leaves no user/assistant pair behind, and
                # the next turn won't send two consecutive user messages.
                if self.messages and self.messages[-1].get("role") == "user":
                    self.messages.pop()

    def close(self) -> None:
        self.messages = [{"role": "system", "content": self.system_prompt}]

class ConversationAdapterClaude:
    """Claude CLI wrapper using `stream-json` with partial messages."""

    provider = EnrichProvider.CLAUDE

    def __init__(self, system_prompt: str):
        self.system_prompt = system_prompt
        self._session_id: Optional[str] = None
        self._proc: Optional[subprocess.Popen] = None

    def turn(self, user_message: str) -> Iterator[str]:
        # --bare isolates the session (no hooks, no CLAUDE.md) but forces
        # ANTHROPIC_API_KEY-only auth. Fall back to a non-bare spawn when the
        # key isn't set so keychain auth keeps working.
        bare = ["--bare"] if os.environ.get("ANTHROPIC_API_KEY") else []
        common = [
            "claude",
            "-p",
            "--output-format", "stream-json",
            "--include-partial-messages",
            *bare,
        ]
        if self._session_id is None:
            argv = [*common, "--system-prompt", self.system_prompt, user_message]
        else:
            argv = [*common, "--resume", self._session_id, user_message]

        try:
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as e:
            log.error("claude CLI not found: %s", e)
            raise RuntimeError("claude CLI not found on PATH") from e

        self._proc = proc
        assert proc.stdout is not None

        drain_thread, stderr_buf = _spawn_stderr_drain(proc)
        error_text: Optional[str] = None
        rc: Optional[int] = None
        try:
            for raw in proc.stdout:
                line = raw.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("skipping malformed claude event: %r", line[:120])
                    continue

                match event.get("type"):
                    case "system" if event.get("subtype") == "init":
                        sid = event.get("session_id")
                        if sid and self._session_id is None:
                            self._session_id = sid
                    case "stream_event":
                        inner = event.get("event") or {}
                        if inner.get("type") == "content_block_delta":
                            delta = inner.get("delta") or {}
                            if delta.get("type") == "text_delta":
                                text = delta.get("text")
                                if text:
                                    yield text
                    case "result":
                        if event.get("is_error"):
                            error_text = event.get("result") or "claude reported error"
        finally:
            try:
                rc = proc.wait()
            except Exception:
                rc = -1
            drain_thread.join(timeout=1)
            _close_pipes(proc)
            self._proc = None

        stderr = "".join(stderr_buf)
        if error_text is not None:
            log.error("claude error: %s", error_text)
            raise RuntimeError(f"claude: {error_text}")
        if rc != 0:
            log.error("claude exited %s: %s", rc, stderr[:500])
            raise RuntimeError(f"claude exited {rc}: {stderr[:200]}")

    def close(self) -> None:
        proc = self._proc
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except (subprocess.TimeoutExpired, ProcessLookupError, OSError):
                try:
                    proc.kill()
                except (ProcessLookupError, OSError):
                    pass
            _close_pipes(proc)
            self._proc = None
        self._session_id = None

class ConversationAdapterCodex:
    """Codex CLI wrapper using `codex exec --json`."""

    provider = EnrichProvider.CODEX

    def __init__(self, system_prompt: str):
        self.system_prompt = system_prompt
        self._session_id: Optional[str] = None
        self._proc: Optional[subprocess.Popen] = None

    def turn(self, user_message: str) -> Iterator[str]:
        if self._session_id is None:
            prompt = f"{self.system_prompt}\n\n{user_message}"
            argv = [
                "codex",
                "exec",
                "--json",
                "--skip-git-repo-check",
                prompt,
            ]
        else:
            argv = [
                "codex",
                "exec",
                "resume",
                self._session_id,
                "--json",
                "--skip-git-repo-check",
                user_message,
            ]

        try:
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as e:
            log.error("codex CLI not found: %s", e)
            raise RuntimeError("codex CLI not found on PATH") from e

        self._proc = proc
        assert proc.stdout is not None

        drain_thread, stderr_buf = _spawn_stderr_drain(proc)
        rc: Optional[int] = None
        try:
            for raw in proc.stdout:
                line = raw.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("skipping malformed codex event: %r", line[:120])
                    continue

                match event.get("type"):
                    case "thread.started":
                        tid = event.get("thread_id")
                        if tid and self._session_id is None:
                            self._session_id = tid
                    case "item.completed":
                        item = event.get("item") or {}
                        if item.get("type") == "agent_message":
                            text = item.get("text")
                            if text:
                                yield text
        finally:
            try:
                rc = proc.wait()
            except Exception:
                rc = -1
            drain_thread.join(timeout=1)
            _close_pipes(proc)
            self._proc = None

        stderr = "".join(stderr_buf)
        if rc != 0:
            log.error("codex exited %s: %s", rc, stderr[:500])
            raise RuntimeError(f"codex exited {rc}: {stderr[:200]}")

    def close(self) -> None:
        proc = self._proc
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except (subprocess.TimeoutExpired, ProcessLookupError, OSError):
                try:
                    proc.kill()
                except (ProcessLookupError, OSError):
                    pass
            _close_pipes(proc)
            self._proc = None
        self._session_id = None
