"""Streaming conversational AI backends.

Sibling of `EnrichAdapter`: where enrichment is a one-shot text rewrite,
these adapters hold a multi-turn session and yield response chunks as they
arrive. `ask.py` drives them from a socket/compose loop."""

import json
import logging
import os
import subprocess
import threading
from dataclasses import dataclass
from typing import Any, Iterator, Optional, Protocol, Union
from enum import StrEnum
import requests

class ConversationProvider(StrEnum):
    HTTP = "http"
    CLAUDE = "claude"
    CODEX = "codex"
    OPENCODE = "opencode"

DEFAULT_CONVERSE_ADAPTER = ConversationProvider.CLAUDE

@dataclass
class ToolCall:
    """A tool-use event surfaced out of `turn()` alongside text chunks.

    The adapter yields `ToolCall` whenever the upstream stream carries a
    tool invocation (OpenAI `delta.tool_calls`, Claude `tool_use` blocks,
    Codex tool_use/shell_command items). The UI treats these as
    visibility-only: we cannot actually gate server-side / in-CLI tool
    execution from the client, so the overlay surfaces a row and lets
    the user dismiss / trust / cancel the turn. `tool_id` is whatever
    stable id the backend gave us so follow-up deltas for the same call
    can be coalesced; `arguments` is the raw JSON string (or shell
    fragment for codex) — the UI does the pretty-printing."""

    tool_id: str
    name: str
    arguments: str

@dataclass
class ThinkingChunk:
    """A streamed chunk of reasoning / extended-thinking content.

    Claude's extended-thinking → `stream_event.event.delta.thinking`.
    OpenAI-compatible chat-completions → `delta.reasoning_content`
    (DeepSeek / GLM / Qwen convention), falling back to `delta.reasoning`
    (OpenRouter) and `delta.thinking` (Anthropic-proxy convention).
    Codex → `item.completed` with `item.type == "reasoning"` and
    `item.text`.

    The UI renders these into a collapsible section inside the active
    assistant card; the section auto-collapses the moment the first
    regular text chunk arrives (visible sign the model has moved from
    thinking to replying)."""

    text: str

# Convenience alias for generator yield types.
TurnChunk = Union[str, ToolCall, ThinkingChunk]

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

    def turn(self, user_message: str) -> Iterator[TurnChunk]:
        """Yield assistant response chunks — `str` for text, `ToolCall`
        for tool-use events. Appends this turn to internal session state."""
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
    """OpenAI-compatible `/chat/completions` with `stream=true`.

    Only `system_prompt` is positional. Everything else arrives via
    `**kwargs` and collapses to the per-field default through the
    `kwargs.get(name) or DEFAULT` idiom — so callers can pass argparse
    values (which are `None` when unset) without replicating defaults at
    every call-site."""

    provider = ConversationProvider.HTTP

    def __init__(self, system_prompt: str, **kwargs):
        self.system_prompt = system_prompt
        self.base_url = kwargs.get("base_url") or "https://ai.kilic.dev/api/v1"
        self.model = kwargs.get("model") or "glm-5.1:cloud"
        self.api_key = kwargs.get("api_key") or ""
        self.user_agent = kwargs.get("user_agent") or "converse/1.0"
        self.temperature = kwargs.get("temperature")
        self.top_p = kwargs.get("top_p")
        self.thinking = kwargs.get("thinking") or "none"
        self.num_ctx = kwargs.get("num_ctx")
        # Operating mode. OpenAI-compatible endpoints don't distinguish
        # plan vs edit (tools execute server-side unconditionally), so
        # we accept the kwarg for API parity with the CLI adapters
        # and expose it as an attribute for callers that want to
        # reflect it in the UI.
        self.mode = kwargs.get("mode") or "plan"
        # OpenWebUI extensions (silently ignored by generic OpenAI servers).
        # tool_ids: server-side tool UUIDs, also accepting the pseudo-ids
        # that represent built-ins (web_search, memory, code_interpreter,
        # image_generation, voice) plus "server:mcp:<id>" for MCP servers.
        # files: [{"type": "file"|"folder"|"collection", "id": "..."}] for RAG context.
        self.tool_ids = kwargs.get("tool_ids")
        self.files = kwargs.get("files")
        self.messages: list[dict] = [
            {"role": "system", "content": system_prompt},
        ]
        # Holds the active requests.Response while a turn is streaming.
        # `cancel()` closes it, which makes iter_lines raise and the
        # generator exits through the finally block.
        self._resp: Optional[requests.Response] = None
        self._cancelled = False

    def turn(self, user_message: str) -> Iterator[TurnChunk]:
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
        # tool_calls come in multiple deltas — OpenAI streams the name +
        # id on the first chunk then appends `arguments` fragments on
        # subsequent chunks, all keyed by the array `index`. We buffer
        # per-index and flush to a `ToolCall` when `finish_reason ==
        # "tool_calls"` or a new index starts.
        pending_tools: dict[int, dict] = {}

        def _flush_tools():
            for idx in sorted(pending_tools):
                t = pending_tools[idx]
                yield ToolCall(
                    tool_id=t.get("id") or f"idx-{idx}",
                    name=t.get("name") or "",
                    arguments=t.get("arguments") or "",
                )
            pending_tools.clear()

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
                choice = choices[0]
                delta = choice.get("delta") or {}
                # Reasoning / thinking tokens. No OpenAI-spec field
                # exists on chat-completions, so we try DeepSeek/GLM's
                # `reasoning_content` first, then OpenRouter's
                # `reasoning`, then Anthropic-proxy's `thinking`.
                thinking = (
                    delta.get("reasoning_content")
                    or delta.get("reasoning")
                    or delta.get("thinking")
                )
                if thinking:
                    yield ThinkingChunk(text=thinking)
                chunk = delta.get("content")
                if chunk:
                    collected.append(chunk)
                    yield chunk
                # Accumulate tool-call deltas by index. The server sends
                # {id, type, function:{name, arguments}} on the opening
                # delta and {function:{arguments:"..."}} chunks after —
                # we merge both into `pending_tools[idx]`.
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    slot = pending_tools.setdefault(idx, {})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["name"] = fn["name"]
                    if fn.get("arguments") is not None:
                        slot["arguments"] = (slot.get("arguments") or "") + fn[
                            "arguments"
                        ]
                if choice.get("finish_reason") == "tool_calls":
                    yield from _flush_tools()
            # End of stream: flush any still-pending tool calls that
            # didn't get an explicit `finish_reason: tool_calls` marker
            # (some OpenAI-compatible servers skip it).
            yield from _flush_tools()
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
    """Claude CLI wrapper using `stream-json` with partial messages.

    Optional MCP wiring:

    - `mcp_config` (`McpConfig` from `lib.mcp`) — aggregates the set
      of MCP servers advertised to claude via `--mcp-config`. Written
      to `$XDG_RUNTIME_DIR/wayland-ask.mcp.json` on first turn and
      reused thereafter. Any server defined here becomes available;
      callers pre-seed what they want (ask.py adds its own approval
      stub, callers can layer github / filesystem / etc. alongside).
    - `permission_tool` — the `mcp__<server>__<tool>` string that
      claude should route permission prompts through. Usually the
      approval tool one of the registered servers exposes. When set
      together with `mcp_config`, claude blocks on that tool before
      running any other tool.

    We deliberately do NOT pass `--strict-mcp-config`: user-level
    config (`~/.claude.json`) and project `.mcp.json` merge with
    ours, so claude's default tool permissions keep working for
    anything we didn't override."""

    provider = ConversationProvider.CLAUDE

    def __init__(self, system_prompt: str, **kwargs):
        self.system_prompt = system_prompt
        self.model = kwargs.get("model") or "sonnet"
        # Maps directly to claude's `--permission-mode`
        # (default | acceptEdits | bypassPermissions | plan). Default
        # is `plan` so claude explores read-only and surfaces write
        # intents through our permission hook instead of silently
        # mutating the workspace.
        self.mode = kwargs.get("mode") or "plan"
        self.mcp_config = kwargs.get("mcp_config")
        self.permission_tool: Optional[str] = kwargs.get("permission_tool")
        self._session_id: Optional[str] = None
        self._proc: Optional[subprocess.Popen] = None
        self._cancelled = False
        # Cached path of the MCP config JSON we write at first turn.
        # Stable per-process so we reuse the same file across turns.
        self._mcp_config_path: Optional[str] = None

    def _mcp_args(self) -> list[str]:
        """Build the `--mcp-config` + `--permission-prompt-tool` flag
        pair from the attached `McpConfig`. Writes the config JSON to
        `$XDG_RUNTIME_DIR/wayland-ask.mcp.json` on first call and
        reuses the same path for follow-up turns."""
        if self.mcp_config is None:
            return []
        if self._mcp_config_path is None:
            runtime = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
            self._mcp_config_path = os.path.join(runtime, "wayland-ask.mcp.json")
            try:
                self.mcp_config.write(self._mcp_config_path)
            except OSError as e:
                log.warning("could not write MCP config: %s", e)
                self._mcp_config_path = None

                return []

        # `--strict-mcp-config` makes OUR config authoritative for
        # this claude subprocess. Without it, user-level
        # (`~/.claude.json`) and project (`.mcp.json`) configs merge
        # in and drown us out — the permission-prompt-tool lookup
        # then can't find the tool we registered. Strict mode is the
        # only way to guarantee `mcp__ask__approve` resolves.
        args = [
            "--mcp-config",
            self._mcp_config_path,
            "--strict-mcp-config",
        ]
        if self.permission_tool:
            args.extend(["--permission-prompt-tool", self.permission_tool])

        return args

    def turn(self, user_message: str) -> Iterator[TurnChunk]:
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
            "--permission-mode",
            self.mode,
            "--output-format",
            "stream-json",
            "--include-partial-messages",
            "--verbose",
            *self._mcp_args(),
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
        # Buffer tool_use content blocks by their `index` — Claude
        # streams `content_block_start` (name + id) then
        # `input_json_delta` fragments then `content_block_stop`. We
        # emit one `ToolCall` when the stop event arrives.
        pending_tools: dict[int, dict] = {}
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
                        itype = inner.get("type")
                        if itype == "content_block_delta":
                            delta = inner.get("delta") or {}
                            dtype = delta.get("type")
                            if dtype == "text_delta":
                                text = delta.get("text")
                                if text:
                                    yield text
                            elif dtype == "thinking_delta":
                                # Extended-thinking stream. We ignore
                                # the sibling `signature_delta` events;
                                # they're for multi-turn continuity on
                                # the API and don't carry user-visible
                                # content.
                                thinking = delta.get("thinking")
                                if thinking:
                                    yield ThinkingChunk(text=thinking)
                            elif dtype == "input_json_delta":
                                idx = inner.get("index", 0)
                                slot = pending_tools.setdefault(idx, {})
                                slot["arguments"] = (slot.get("arguments") or "") + (
                                    delta.get("partial_json") or ""
                                )
                        elif itype == "content_block_start":
                            block = inner.get("content_block") or {}
                            if block.get("type") == "tool_use":
                                idx = inner.get("index", 0)
                                pending_tools[idx] = {
                                    "id": block.get("id") or f"idx-{idx}",
                                    "name": block.get("name") or "",
                                    "arguments": "",
                                }
                        elif itype == "content_block_stop":
                            idx = inner.get("index", 0)
                            slot = pending_tools.pop(idx, None)
                            # When MCP permission gating is active the
                            # approval tool already fired a row BEFORE
                            # the tool ran; emitting the in-stream
                            # event here would duplicate it. Gating
                            # path wins; in-stream tool_use becomes a
                            # no-op.
                            if (
                                slot
                                and slot.get("name")
                                and not (self.mcp_config and self.permission_tool)
                            ):
                                yield ToolCall(
                                    tool_id=slot["id"],
                                    name=slot["name"],
                                    arguments=slot.get("arguments") or "",
                                )
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

    def __init__(self, system_prompt: str, **kwargs):
        self.system_prompt = system_prompt
        self.model = kwargs.get("model") or "gpt-5.4"
        # Codex has no literal "plan" flag — its closest equivalent is
        # a read-only sandbox that blocks filesystem writes. We map
        # mode "plan" → `--sandbox read-only` and leave any other mode
        # string to pass through verbatim (so callers can pick
        # `workspace-write` / `danger-full-access` if they want).
        self.mode = kwargs.get("mode") or "plan"
        self._session_id: Optional[str] = None
        self._proc: Optional[subprocess.Popen] = None
        self._cancelled = False

    def _sandbox_args(self) -> list[str]:
        sandbox = "read-only" if self.mode == "plan" else self.mode

        return ["--sandbox", sandbox]

    def turn(self, user_message: str) -> Iterator[TurnChunk]:
        self._cancelled = False
        if self._session_id is None:
            prompt = f"{self.system_prompt}\n\n{user_message}"
            argv = [
                "codex",
                "exec",
                "--model",
                self.model,
                *self._sandbox_args(),
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
                *self._sandbox_args(),
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
                        itype = item.get("type")
                        if itype == "agent_message":
                            text = item.get("text")
                            if text:
                                yield text
                        elif itype == "reasoning":
                            # Codex emits reasoning as a single item
                            # (no streaming deltas like Claude), so it
                            # arrives whole.
                            text = item.get("text") or item.get("content") or ""
                            if text:
                                yield ThinkingChunk(text=str(text))
                        elif itype in ("tool_use", "tool_call", "shell_command"):
                            # Codex stringifies the invocation differently per
                            # item type. Prefer structured fields, fall back
                            # to whatever is in the item as JSON so the user
                            # sees *something* useful in the permission row.
                            args = (
                                item.get("arguments")
                                or item.get("input")
                                or item.get("command")
                                or ""
                            )
                            if isinstance(args, (dict, list)):
                                args = json.dumps(args)
                            yield ToolCall(
                                tool_id=item.get("id") or itype,
                                name=item.get("name") or itype,
                                arguments=str(args),
                            )
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

class ConversationAdapterOpenCode:
    """OpenCode CLI wrapper using `opencode run --format json`.

    OpenCode reads `$OPENCODE_CONFIG` at startup — the JSON defines
    providers, models, permissions, and (optionally) MCP servers. We
    default to the `kilic.json` config shipped alongside the nvim
    dotfiles. Models are addressed as `<provider>/<model>`; our
    default provider is `kilic`, so the default model is
    `kilic/glm-5.1:cloud`.

    Session continuity: first turn captures `sessionID` from the
    initial `step_start` event, follow-up turns pass `--session <id>`.

    MCP merging: when an `mcp_config` (the lib.mcp `McpConfig` we use
    for claude) is supplied, we translate its server definitions into
    OpenCode's `mcp` schema and splice them into a temp config file
    that extends the base config. That way the ask overlay's MCP
    approve/question tools are available to OpenCode too."""

    provider = ConversationProvider.OPENCODE

    DEFAULT_MODEL = "glm-5.1:cloud"
    DEFAULT_PROVIDER = "kilic"

    def __init__(self, system_prompt: str, **kwargs):
        self.system_prompt = system_prompt
        self.model = kwargs.get("model") or self.DEFAULT_MODEL
        # OpenCode's `plan` agent is a built-in read-only mode. We
        # pass it through `--agent` when mode == "plan"; otherwise
        # the CLI uses the default agent.
        self.mode = kwargs.get("mode") or "plan"
        self.config_path = kwargs.get("config_path") or os.path.expanduser(
            "~/.config/nvim/utils/agents/opencode/kilic.json"
        )
        self.provider_name = kwargs.get("provider_name") or "kilic"
        self.mcp_config = kwargs.get("mcp_config")
        self._session_id: Optional[str] = None
        self._proc: Optional[subprocess.Popen] = None
        self._cancelled = False
        self._effective_config_path: Optional[str] = None

    def turn(self, user_message: str) -> Iterator[TurnChunk]:
        self._cancelled = False
        model_spec = f"{self.provider_name}/{self.model}"
        argv = [
            "opencode",
            "run",
            "--format",
            "json",
            "--model",
            model_spec,
        ]
        if self.mode == "plan":
            argv.extend(["--agent", "plan"])
        if self._session_id:
            argv.extend(["--session", self._session_id])

        # First turn carries the system prompt inline (opencode run
        # has no dedicated `--system-prompt`). Subsequent turns rely
        # on the resumed session's context.
        if self._session_id is None:
            prompt = f"{self.system_prompt}\n\n{user_message}"
        else:
            prompt = user_message
        argv.append(prompt)

        # Resolve `OPENCODE_CONFIG`. Missing base config → run without
        # --config. `mcp_config` supplied → splice our MCP servers
        # into OpenCode's `mcp` schema and cache the merged file so
        # follow-up turns reuse it.
        env = os.environ.copy()
        cfg: Optional[str]
        if not os.path.exists(self.config_path):
            log.warning(
                "opencode config missing at %s — running without --config",
                self.config_path,
            )
            cfg = None
        elif self.mcp_config is None:
            cfg = self.config_path
        elif self._effective_config_path is not None:
            cfg = self._effective_config_path
        else:
            try:
                with open(self.config_path, encoding="utf-8") as f:
                    base = json.load(f)
                merged = dict(base)
                existing_mcp = dict(merged.get("mcp") or {})
                # Translate `{"mcpServers": {"ask": {command, args, env}}}`
                # into OpenCode's `mcp` schema: `{type, command: [cmd, *args],
                # environment, enabled}`.
                for name, spec in self.mcp_config.to_dict().get(
                    "mcpServers", {}
                ).items():
                    existing_mcp.setdefault(
                        name,
                        {
                            "type": "local",
                            "command": [spec.get("command", "")]
                            + list(spec.get("args") or []),
                            "environment": dict(spec.get("env") or {}),
                            "enabled": True,
                        },
                    )
                merged["mcp"] = existing_mcp
                runtime = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
                self._effective_config_path = os.path.join(
                    runtime, "wayland-ask.opencode.json"
                )
                with open(self._effective_config_path, "w", encoding="utf-8") as f:
                    json.dump(merged, f)
                cfg = self._effective_config_path
            except (OSError, json.JSONDecodeError) as e:
                log.warning(
                    "couldn't prepare merged opencode config %s: %s",
                    self.config_path,
                    e,
                )
                cfg = self.config_path
        if cfg:
            env["OPENCODE_CONFIG"] = cfg

        try:
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                env=env,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as e:
            log.error("opencode CLI not found: %s", e)
            raise RuntimeError("opencode CLI not found on PATH") from e

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
                    log.warning("skipping malformed opencode event: %r", line[:120])
                    continue
                etype = event.get("type")
                part = event.get("part") or {}
                if etype == "step_start":
                    sid = part.get("sessionID") or event.get("sessionID")
                    if sid and self._session_id is None:
                        self._session_id = sid
                elif etype == "text":
                    # OpenCode emits text as a single aggregated event
                    # per step (no per-token deltas). Still yields as
                    # a str for the overlay; just won't feel as
                    # progressive as claude's stream-json.
                    text = part.get("text") or ""
                    if text:
                        yield text
                elif etype == "reasoning":
                    thinking = part.get("text") or part.get("content") or ""
                    if thinking:
                        yield ThinkingChunk(text=str(thinking))
                elif etype in ("tool", "tool-start", "tool-use"):
                    tool_name = (
                        part.get("tool")
                        or part.get("name")
                        or event.get("tool")
                        or etype
                    )
                    tool_input = (
                        part.get("input")
                        or part.get("arguments")
                        or part.get("args")
                        or {}
                    )
                    if isinstance(tool_input, (dict, list)):
                        tool_args = json.dumps(tool_input)
                    else:
                        tool_args = str(tool_input)
                    yield ToolCall(
                        tool_id=part.get("id") or f"oc-{etype}",
                        name=str(tool_name),
                        arguments=tool_args,
                    )
                elif etype == "step_finish":
                    # One step complete. OpenCode may emit multiple
                    # step_* pairs per turn when tool calls happen;
                    # just keep consuming.
                    continue
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
            log.error("opencode exited %s: %s", rc, stderr[:500])
            raise RuntimeError(f"opencode exited {rc}: {stderr[:200]}")

    def cancel(self) -> None:
        self._cancelled = True
        _terminate_proc(self._proc)

    def close(self) -> None:
        self.cancel()
        self._proc = None
        self._session_id = None
