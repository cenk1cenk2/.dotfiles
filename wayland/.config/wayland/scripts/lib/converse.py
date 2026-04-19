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
from enum import StrEnum
import requests

class ConversationProvider(StrEnum):
    HTTP = "http"
    CLAUDE = "claude"
    CODEX = "codex"

DEFAULT_CONVERSE_ADAPTER = ConversationProvider.CLAUDE
DEFAULT_CONVERSE_MODEL = "opus:4.7"
DEFAULT_CONVERSE_MODEL_CLAUDE = "opus"
DEFAULT_CONVERSE_MODEL_CODEX = "gpt-5"
DEFAULT_CONVERSE_BASE_URL = "https://ai.kilic.dev/api/v1"

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

def _terminate_proc(proc: Optional[subprocess.Popen]) -> None:
    """Send SIGTERM, escalate to SIGKILL after a short grace period. Used
    by `cancel()` / `close()` on the CLI-backed adapters."""
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=2)
    except (subprocess.TimeoutExpired, ProcessLookupError, OSError):
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass
    _close_pipes(proc)

def _cleanup_session_files(root: str, patterns: list[str]) -> None:
    """Best-effort delete of any transcript/session files matching the given
    glob patterns under `root`. Silent on errors — session cleanup must
    never block or fail a hard-close path."""
    import pathlib

    base = pathlib.Path(root).expanduser()
    if not base.exists():
        return
    for pattern in patterns:
        for path in base.rglob(pattern):
            if not path.is_file():
                continue
            try:
                path.unlink()
                log.info("removed session file: %s", path)
            except OSError as e:
                log.debug("could not remove %s: %s", path, e)

class ConversationAdapter(Protocol):
    """Streaming, stateful AI backend. Each `turn()` extends the session."""

    provider: ConversationProvider
    model: str

    def turn(self, user_message: str) -> Iterator[str]:
        """Yield assistant response chunks. Appends this turn to internal
        session state."""
        ...

    def cancel(self) -> None:
        """Abort the in-flight turn (if any). Safe to call while no turn
        is active. The generator returned by `turn()` will stop yielding
        and return normally."""
        ...

    def close(self) -> None:
        """Release any subprocess / session handles."""
        ...

class ConversationAdapterHttp:
    """OpenAI-compatible `/chat/completions` with `stream=true`."""

    provider = ConversationProvider.HTTP

    def __init__(
        self,
        system_prompt: str,
        *,
        base_url: str = DEFAULT_CONVERSE_BASE_URL,
        model: str = DEFAULT_CONVERSE_MODEL,
        api_key: str = "",
        user_agent: str = "converse/1.0",
        temperature: Optional[float] = None,
        top_p: Optional[float] = None,
        thinking: str = "none",
        num_ctx: Optional[int] = None,
        tool_ids: Optional[list[str]] = None,
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
        # tool_ids: server-side tool UUIDs, also accepting the pseudo-ids
        # that represent built-ins (web_search, memory, code_interpreter,
        # image_generation, voice) plus "server:mcp:<id>" for MCP servers.
        # files: [{"type": "file"|"folder"|"collection", "id": "..."}] for RAG context.
        self.tool_ids = tool_ids
        self.files = files
        self.messages: list[dict] = [
            {"role": "system", "content": system_prompt},
        ]
        # Holds the active requests.Response while a turn is streaming.
        # `cancel()` closes it, which makes iter_lines raise and the
        # generator exits through the finally block.
        self._resp: Optional[requests.Response] = None
        self._cancelled = False

    def turn(self, user_message: str) -> Iterator[str]:
        self._cancelled = False
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
        if self.files:
            body["files"] = self.files

        # Debug-level body log so `ask.py -v` can show exactly what we
        # sent — useful when a server-side feature (web_search, tools)
        # isn't responding the way we expected.
        log.debug(
            "http request: %s",
            json.dumps({**body, "messages": f"<{len(body['messages'])} msgs>"}),
        )

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

        self._resp = resp
        collected: list[str] = []
        stream_ok = False
        try:
            for line in resp.iter_lines(decode_unicode=True):
                if self._cancelled:
                    break
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
        except requests.RequestException as e:
            # cancel() closes the response mid-iteration, which surfaces
            # here as a connection error. Swallow it if we asked for the
            # cancel; otherwise re-raise so callers see the real failure.
            if not self._cancelled:
                raise
            log.debug("http stream cancelled: %s", e)
        finally:
            try:
                resp.close()
            except Exception:
                pass
            self._resp = None
            if stream_ok and collected:
                self.messages.append(
                    {"role": "assistant", "content": "".join(collected)},
                )
            elif self._cancelled and collected:
                # Preserve whatever we streamed before the cancel so the
                # next turn has the partial response in context.
                self.messages.append(
                    {"role": "assistant", "content": "".join(collected)},
                )
            else:
                # Roll back the user turn so history stays consistent: a
                # failed stream leaves no user/assistant pair behind, and
                # the next turn won't send two consecutive user messages.
                if self.messages and self.messages[-1].get("role") == "user":
                    self.messages.pop()

    def cancel(self) -> None:
        self._cancelled = True
        resp = self._resp
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass

    def close(self) -> None:
        self.cancel()
        self.messages = [{"role": "system", "content": self.system_prompt}]

class ConversationAdapterClaude:
    """Claude CLI wrapper using `stream-json` with partial messages."""

    provider = ConversationProvider.CLAUDE

    def __init__(
        self,
        system_prompt: str,
        *,
        model: str = DEFAULT_CONVERSE_MODEL_CLAUDE,
    ):
        self.system_prompt = system_prompt
        self.model = model
        self._session_id: Optional[str] = None
        self._proc: Optional[subprocess.Popen] = None
        self._cancelled = False

    def turn(self, user_message: str) -> Iterator[str]:
        self._cancelled = False
        # --bare isolates the session (no hooks, no CLAUDE.md) but forces
        # ANTHROPIC_API_KEY-only auth. Fall back to a non-bare spawn when the
        # key isn't set so keychain auth keeps working.
        bare = ["--bare"] if os.environ.get("ANTHROPIC_API_KEY") else []
        common = [
            "claude",
            "-p",
            "--model",
            self.model,
            "--output-format",
            "stream-json",
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
        try:
            for raw in proc.stdout:
                if self._cancelled:
                    break
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

        if self._cancelled:
            return
        stderr = "".join(stderr_buf)
        if error_text is not None:
            log.error("claude error: %s", error_text)
            raise RuntimeError(f"claude: {error_text}")
        if rc != 0:
            log.error("claude exited %s: %s", rc, stderr[:500])
            raise RuntimeError(f"claude exited {rc}: {stderr[:200]}")

    def cancel(self) -> None:
        self._cancelled = True
        _terminate_proc(self._proc)

    def close(self) -> None:
        self.cancel()
        self._proc = None
        session_id = self._session_id
        self._session_id = None
        if session_id:
            # `claude -p` writes the transcript to ~/.claude/projects/<cwd-hash>/
            # <session_id>.jsonl. We created it, we clean it up.
            _cleanup_session_files("~/.claude/projects", [f"{session_id}.jsonl"])

class ConversationAdapterCodex:
    """Codex CLI wrapper using `codex exec --json`."""

    provider = ConversationProvider.CODEX

    def __init__(
        self,
        system_prompt: str,
        *,
        model: str = DEFAULT_CONVERSE_MODEL_CODEX,
    ):
        self.system_prompt = system_prompt
        self.model = model
        self._session_id: Optional[str] = None
        self._proc: Optional[subprocess.Popen] = None
        self._cancelled = False

    def turn(self, user_message: str) -> Iterator[str]:
        self._cancelled = False
        if self._session_id is None:
            prompt = f"{self.system_prompt}\n\n{user_message}"
            argv = [
                "codex",
                "exec",
                "--model",
                self.model,
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
                "--model",
                self.model,
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
        try:
            for raw in proc.stdout:
                if self._cancelled:
                    break
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

        if self._cancelled:
            return
        stderr = "".join(stderr_buf)
        if rc != 0:
            log.error("codex exited %s: %s", rc, stderr[:500])
            raise RuntimeError(f"codex exited {rc}: {stderr[:200]}")

    def cancel(self) -> None:
        self._cancelled = True
        _terminate_proc(self._proc)

    def close(self) -> None:
        self.cancel()
        self._proc = None
        thread_id = self._session_id
        self._session_id = None
        if thread_id:
            # `codex exec --json` writes a rollout file under
            # ~/.codex/sessions/YYYY/MM/DD/ containing the thread_id in its
            # name. Glob-match and drop anything that mentions our thread.
            _cleanup_session_files("~/.codex/sessions", [f"*{thread_id}*"])
