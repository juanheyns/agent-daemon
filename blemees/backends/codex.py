"""Codex backend (`codex mcp-server`).

Wraps a long-running `codex mcp-server` child speaking JSON-RPC 2.0 over
NDJSON stdio. Each user turn becomes a `tools/call` (`codex` for the
first turn, `codex-reply` for subsequent turns) and the resulting
`codex/event` notification stream is translated to `agent.*` frames via
:class:`blemees.backends.translate_codex.CodexTranslator`. The terminal
JSON-RPC response is folded into the synthesised `agent.result`.

Responsibilities:
    * Spawn the child and run the MCP `initialize` handshake.
    * Verify `codex` / `codex-reply` are present on `tools/list`.
    * Allocate per-turn request ids; reject re-entry while a turn is
      live.
    * Translate every `codex/event` notification into agent.* frames
      and synthesise `agent.result` from the JSON-RPC response.
    * Cancel an in-flight turn via `notifications/cancelled`.
    * Detect auth-related JSON-RPC errors and surface as
      `blemeesd.error{auth_failed}`.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import signal
import subprocess as _stdlib_subprocess
import time
from collections import deque
from pathlib import Path
from typing import Any

from ..errors import (
    AUTH_FAILED,
    BACKEND_CRASHED,
    SessionBusyError,
    SpawnFailedError,
)
from . import EventCallback
from .translate_codex import CodexTranslator

# JSON-RPC handshake constants. `protocolVersion` matches what the
# captured trace uses (Codex 0.125.x echoes the same string back).
_MCP_PROTOCOL_VERSION = "2024-11-05"
_INITIALIZE_TIMEOUT_S = 10.0
_TOOLS_LIST_TIMEOUT_S = 10.0

# JSON-RPC error codes Codex returns for auth failures.
#
# Codex's MCP server uses the JSON-RPC 2.0 server-error band
# (-32099..-32000) for application-level errors. Two codes turn up in
# practice for authn/authz failures:
#
#   -32001  — generic auth failure (token missing / expired).
#   -32002  — server error emitted when the upstream model API
#             rejects credentials (401/403 from OpenAI's edge).
#
# We also accept the bare HTTP status codes 401 / 403 in case Codex
# (or a stub used in tests) surfaces them directly. As a defence-in-
# depth check we scan the error message body for known phrases — the
# code path may vary across Codex releases, but the wording stays
# stable enough that "please run `codex login`" / "401" reliably
# indicate the same condition.
_AUTH_ERROR_CODES: frozenset[int] = frozenset({-32001, -32002, 401, 403})
_AUTH_FAIL_PATTERNS: tuple[str, ...] = (
    "401",
    "403",
    "not authenticated",
    "auth required",
    "please run `codex login`",
    "please run codex login",
    "missing api key",
    "OPENAI_API_KEY",
    "unauthorized",
)


def _looks_like_auth_failure(err: dict[str, Any]) -> bool:
    """Heuristic: does the JSON-RPC error look like an auth failure?

    Returns True for any of:
      * top-level ``code`` in :data:`_AUTH_ERROR_CODES`,
      * structured ``data.code`` in :data:`_AUTH_ERROR_CODES` (Codex
        sometimes nests upstream HTTP status under ``error.data``),
      * structured ``data.type`` of ``"auth_failed"`` /
        ``"unauthorized"``,
      * any of :data:`_AUTH_FAIL_PATTERNS` in the message body.
    """
    if not isinstance(err, dict):
        return False
    code = err.get("code")
    if isinstance(code, int) and code in _AUTH_ERROR_CODES:
        return True
    data = err.get("data")
    if isinstance(data, dict):
        nested_code = data.get("code")
        if isinstance(nested_code, int) and nested_code in _AUTH_ERROR_CODES:
            return True
        nested_type = data.get("type")
        if isinstance(nested_type, str) and nested_type.lower() in {
            "auth_failed",
            "unauthorized",
            "missing_api_key",
        }:
            return True
    message = err.get("message", "")
    if isinstance(message, str):
        lowered = message.lower()
        for pattern in _AUTH_FAIL_PATTERNS:
            if pattern.lower() in lowered:
                return True
    return False


_VERSION_RE = re.compile(r"\b(\d+(?:\.\d+){1,3}\S*)")


class _StderrRateLimiter:
    """Rolling-window line limiter — same shape as the Claude backend."""

    def __init__(self, max_lines: int, window_s: float) -> None:
        self._max = max_lines
        self._window = window_s
        self._ts: deque[float] = deque()
        self.dropped = 0

    def allow(self) -> bool:
        now = time.monotonic()
        window_start = now - self._window
        while self._ts and self._ts[0] < window_start:
            self._ts.popleft()
        if len(self._ts) >= self._max:
            self.dropped += 1
            return False
        self._ts.append(now)
        return True


class CodexBackend:
    """One child process running ``codex mcp-server``."""

    backend = "codex"

    def __init__(
        self,
        *,
        session_id: str,
        argv: list[str],
        cwd: str | None,
        options: dict[str, Any],
        on_event: EventCallback,
        logger,
        stderr_rate_lines: int = 50,
        stderr_rate_window_s: float = 10.0,
        include_raw_events: bool = False,
        thread_id: str | None = None,
    ) -> None:
        self.session_id = session_id
        self._argv = argv
        self._cwd = cwd
        self._options = options
        self._on_event = on_event
        self._log = logger.bind(session_id=session_id, backend=self.backend)
        self._stderr_limit = _StderrRateLimiter(stderr_rate_lines, stderr_rate_window_s)
        self._include_raw = include_raw_events

        self.proc: asyncio.subprocess.Process | None = None
        self.pid: int | None = None
        self.turn_active: bool = False
        self._closing: bool = False
        self._stderr_tail: deque[str] = deque(maxlen=20)
        self._reader_tasks: list[asyncio.Task] = []
        self._stdin_lock = asyncio.Lock()

        # JSON-RPC plumbing.
        self._next_id: int = 1
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        # Request id of the in-flight `tools/call`, if any. Cleared on
        # turn end.
        self._active_turn_id: int | None = None
        # Cached threadId from `session_configured`. None until the first
        # turn produces it; subsequent turns route via `codex-reply`.
        # On resume the daemon supplies the previously-known threadId,
        # so the very first turn after respawn already routes correctly.
        self._thread_id: str | None = thread_id

        # Translator owns the per-turn buffering for the synthesised
        # `agent.result` (see translate_codex.CodexTranslator).
        self._translator = CodexTranslator(include_raw=include_raw_events)

        # Set when the initialize handshake has completed.
        self._initialized: bool = False
        self._auth_emitted: bool = False
        # Marks the active turn as cancelled by the client, so the
        # eventual JSON-RPC response is converted into
        # ``agent.result{subtype:"interrupted"}`` regardless of what
        # Codex returns.
        self._cancel_active: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def spawn(self) -> None:
        """Launch `codex mcp-server` and run the MCP handshake.

        Raises :class:`SpawnFailedError` on OS errors, handshake
        failure, or missing `codex` / `codex-reply` tools.
        """
        self._log.info(
            "backend.spawn",
            argv_head=self._argv[:4],
            cwd=self._cwd,
        )
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *self._argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._cwd,
            )
        except (FileNotFoundError, PermissionError, OSError) as exc:
            raise SpawnFailedError(f"failed to launch codex: {exc}") from exc
        self.pid = self.proc.pid
        self._log = self._log.bind(pid=self.pid)
        self._reader_tasks = [
            asyncio.create_task(self._read_stdout(), name=f"codex-stdout-{self.session_id}"),
            asyncio.create_task(self._read_stderr(), name=f"codex-stderr-{self.session_id}"),
            asyncio.create_task(self._watch_exit(), name=f"codex-exit-{self.session_id}"),
        ]

        try:
            await self._do_initialize()
            await self._send_notification("notifications/initialized")
            await self._verify_tools()
        except SpawnFailedError:
            self._closing = True
            await self._kill()
            await self._drain_readers()
            raise
        self._initialized = True

    async def _do_initialize(self) -> None:
        try:
            init_result = await asyncio.wait_for(
                self._call(
                    "initialize",
                    {
                        "protocolVersion": _MCP_PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": {"name": "blemeesd", "version": "0"},
                    },
                ),
                timeout=_INITIALIZE_TIMEOUT_S,
            )
        except TimeoutError as exc:
            raise SpawnFailedError("codex initialize timed out") from exc
        except _RpcError as exc:
            raise SpawnFailedError(f"codex initialize failed: {exc.message}") from exc
        if not isinstance(init_result, dict):
            raise SpawnFailedError("codex initialize returned non-object")

    async def _verify_tools(self) -> None:
        try:
            tools_result = await asyncio.wait_for(
                self._call("tools/list"),
                timeout=_TOOLS_LIST_TIMEOUT_S,
            )
        except TimeoutError as exc:
            raise SpawnFailedError("codex tools/list timed out") from exc
        except _RpcError as exc:
            raise SpawnFailedError(f"codex tools/list failed: {exc.message}") from exc
        tool_names = {
            t.get("name") for t in (tools_result.get("tools") or []) if isinstance(t, dict)
        }
        for required in ("codex", "codex-reply"):
            if required not in tool_names:
                raise SpawnFailedError(f"codex mcp-server missing required tool: {required!r}")

    # ------------------------------------------------------------------
    # Turn I/O
    # ------------------------------------------------------------------

    async def send_user_turn(self, message: dict[str, Any]) -> None:
        """Issue one ``tools/call`` for ``message``.

        Raises :class:`SessionBusyError` if a turn is already in flight,
        :class:`SpawnFailedError` if the transport has died, and a
        :class:`SpawnFailedError` (re-using the code) if the message
        carries non-text content blocks Codex can't accept yet.
        """
        if self.proc is None or self.proc.returncode is not None:
            raise SpawnFailedError("subprocess not running")
        if self.turn_active:
            raise SessionBusyError(self.session_id)

        prompt = _flatten_content_to_text(message.get("content"))
        if prompt is None:
            from ..errors import ProtocolError

            raise ProtocolError(
                "codex backend accepts text-only content; non-text blocks are not yet supported",
            )

        if self._thread_id is None:
            tool_name = "codex"
            args = build_codex_tool_args(self._options, prompt=prompt)
        else:
            tool_name = "codex-reply"
            args = {"prompt": prompt, "threadId": self._thread_id}

        async with self._stdin_lock:
            self.turn_active = True
            req_id = self._allocate_id()
            self._active_turn_id = req_id
            try:
                await self._write_request(
                    req_id, "tools/call", {"name": tool_name, "arguments": args}
                )
            except (ConnectionResetError, BrokenPipeError) as exc:
                self.turn_active = False
                self._active_turn_id = None
                raise SpawnFailedError(f"codex stdin write failed: {exc}") from exc

    # ------------------------------------------------------------------
    # Interrupt + close
    # ------------------------------------------------------------------

    async def interrupt(self) -> bool:
        """Cancel the in-flight turn via ``notifications/cancelled``.

        Per spec §9.4 / agent-events.md, this does *not* kill the child;
        Codex emits a final response which the reader translates into
        ``agent.result{subtype:"interrupted"}``. Returns ``False`` if no
        turn was in flight.
        """
        if not self.turn_active or self._active_turn_id is None:
            return False
        self._cancel_active = True
        try:
            await self._send_notification(
                "notifications/cancelled",
                {"requestId": self._active_turn_id, "reason": "user_interrupt"},
            )
        except (ConnectionResetError, BrokenPipeError):
            # Transport dead — fall through to kill so the daemon can
            # respawn cleanly. Surfaced via the watcher's BACKEND_CRASHED
            # path on actual exit.
            await self._kill()
            return True
        return True

    async def _kill(self, *, grace_ms: int = 500) -> None:
        if self.proc is None or self.proc.returncode is not None:
            return
        try:
            self.proc.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(self.proc.wait(), timeout=grace_ms / 1000.0)
        except TimeoutError:
            try:
                self.proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=1.0)
            except TimeoutError:  # pragma: no cover - defensive
                self._log.error("backend.kill_failed")

    async def close(self) -> None:
        self._closing = True
        await self._kill()
        await self._drain_readers()
        # Resolve any pending RPC futures so callers don't hang.
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(_RpcError("codex backend closed"))
        self._pending.clear()

    async def wait_for_exit(self, timeout: float) -> bool:
        if self.proc is None or self.proc.returncode is not None:
            return True
        try:
            await asyncio.wait_for(self.proc.wait(), timeout=timeout)
            return True
        except TimeoutError:
            return False

    async def _drain_readers(self) -> None:
        for task in self._reader_tasks:
            if not task.done():
                try:
                    await asyncio.wait_for(task, timeout=2.0)
                except TimeoutError:  # pragma: no cover - defensive
                    task.cancel()
        self._reader_tasks = []

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    # ------------------------------------------------------------------
    # JSON-RPC plumbing
    # ------------------------------------------------------------------

    def _allocate_id(self) -> int:
        out = self._next_id
        self._next_id += 1
        return out

    async def _call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send a JSON-RPC request and await its result. Internal — used
        for the handshake (`initialize`, `tools/list`). User-turn
        `tools/call`s are sent fire-and-forget and resolved by the
        reader directly into agent.* frames.
        """
        req_id = self._allocate_id()
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[req_id] = fut
        try:
            await self._write_request(req_id, method, params)
            return await fut
        finally:
            self._pending.pop(req_id, None)

    async def _write_request(self, req_id: int, method: str, params: dict[str, Any] | None) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            msg["params"] = params
        await self._write_line(msg)

    async def _send_notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        await self._write_line(msg)

    async def _write_line(self, msg: dict[str, Any]) -> None:
        if self.proc is None or self.proc.stdin is None:
            raise SpawnFailedError("codex stdin not open")
        line = (json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        self.proc.stdin.write(line)
        await self.proc.stdin.drain()

    # ------------------------------------------------------------------
    # Readers
    # ------------------------------------------------------------------

    async def _read_stdout(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        stdout = self.proc.stdout
        while True:
            try:
                raw = await stdout.readline()
            except (asyncio.LimitOverrunError, ValueError):
                self._log.warning("backend.stdout_overrun")
                await stdout.read(1)
                continue
            if not raw:
                return
            line = raw.rstrip(b"\r\n")
            if not line:
                continue
            text = line.decode("utf-8", errors="replace")
            # LSP framing detector — Phase 0 confirmed Codex uses NDJSON.
            # If a future release switches to `Content-Length:` headers,
            # surface a notice and stop processing.
            if text.startswith("Content-Length:"):
                await self._on_event(
                    {
                        "type": "agent.notice",
                        "level": "warn",
                        "category": "codex_unsupported_framing",
                        "text": "codex switched to LSP framing; daemon needs upgrade",
                        "backend": self.backend,
                    }
                )
                return
            try:
                obj = json.loads(text)
            except json.JSONDecodeError:
                self._log.warning("backend.non_json_stdout", length=len(text))
                continue
            if not isinstance(obj, dict):
                continue
            await self._dispatch_rpc(obj)

    async def _dispatch_rpc(self, obj: dict[str, Any]) -> None:
        # Notification (no `id` key, has `method`).
        method = obj.get("method")
        if isinstance(method, str) and "id" not in obj:
            await self._handle_notification(method, obj.get("params") or {})
            return

        # Response (has `id`, has `result` or `error`).
        msg_id = obj.get("id")
        if not isinstance(msg_id, int):
            return

        # In-flight user-turn response → terminal agent.result.
        if msg_id == self._active_turn_id:
            await self._handle_turn_response(msg_id, obj)
            return

        # Handshake / internal call response.
        fut = self._pending.get(msg_id)
        if fut is None or fut.done():
            return
        if "error" in obj:
            err = obj["error"] or {}
            fut.set_exception(_RpcError(err.get("message", "rpc error"), data=err))
        else:
            fut.set_result(obj.get("result") or {})

    async def _handle_notification(self, method: str, params: dict[str, Any]) -> None:
        # Codex emits its events as `codex/event`; some MCP servers wrap
        # them under `notifications/codex/event`. Accept either.
        if method in ("codex/event", "notifications/codex/event"):
            msg = params.get("msg")
            if isinstance(msg, dict):
                meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else None
                # Pick up the threadId from `_meta` if we don't have it
                # from `session_configured` yet — reply turns need it.
                if self._thread_id is None and meta and isinstance(meta.get("threadId"), str):
                    self._thread_id = meta["threadId"]
                # Filter late-arriving events tagged with a stale
                # requestId. After we fold ``turn_aborted`` into
                # ``agent.result{interrupted}`` Codex 0.125.x keeps
                # streaming events for the cancelled call (interleaved
                # with events for the new call). Routing them through
                # the translator would surface stray ``agent.delta`` /
                # ``agent.message`` frames *and* clobber the new turn's
                # buffered ``task_complete``/``token_count``.
                req_id = meta.get("requestId") if isinstance(meta, dict) else None
                if (
                    isinstance(req_id, int)
                    and self._active_turn_id is not None
                    and req_id != self._active_turn_id
                ):
                    return
                for frame in self._translator.translate_event(msg, meta=meta):
                    if (
                        self._thread_id is None
                        and frame.get("type") == "agent.system_init"
                        and isinstance(frame.get("native_session_id"), str)
                    ):
                        self._thread_id = frame["native_session_id"]
                    await self._emit_translated(frame)
                # Codex 0.125.x signals a successful cancellation via
                # ``turn_aborted`` and *does not* always follow with a
                # JSON-RPC response. Finalize the turn here so the
                # client sees ``agent.result{subtype:"interrupted"}``
                # without waiting for a phantom reply.
                if msg.get("type") == "turn_aborted" and self.turn_active:
                    aborted_frame = self._translator.finalize_interrupted()
                    self.turn_active = False
                    self._active_turn_id = None
                    self._cancel_active = False
                    await self._emit_translated(aborted_frame)
            return
        # Other MCP notifications — surface as a notice rather than
        # silently dropping them.
        await self._emit_translated(
            {
                "type": "agent.notice",
                "level": "info",
                "category": f"codex_notif_{method}",
            }
        )

    async def _handle_turn_response(self, msg_id: int, obj: dict[str, Any]) -> None:
        if self._cancel_active:
            frame = self._translator.finalize_interrupted()
        elif "error" in obj:
            err = obj.get("error") or {}
            await self._maybe_emit_auth_error(err)
            frame = self._translator.finalize_error(err if isinstance(err, dict) else None)
        else:
            frame = self._translator.finalize_success()
        self.turn_active = False
        self._active_turn_id = None
        self._cancel_active = False
        await self._emit_translated(frame)

    async def _emit_translated(self, frame: dict[str, Any]) -> None:
        frame["backend"] = self.backend
        await self._on_event(frame)

    async def _maybe_emit_auth_error(self, err: dict[str, Any]) -> None:
        if self._auth_emitted:
            return
        if not _looks_like_auth_failure(err):
            return
        self._auth_emitted = True
        await self._on_event(
            {
                "type": "blemeesd.error",
                "session_id": self.session_id,
                "backend": self.backend,
                "code": AUTH_FAILED,
                "message": "Run `codex login` to re-authenticate.",
            }
        )

    async def _read_stderr(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        stderr = self.proc.stderr
        while True:
            try:
                raw = await stderr.readline()
            except (asyncio.LimitOverrunError, ValueError):
                await stderr.read(1)
                continue
            if not raw:
                return
            line = raw.rstrip(b"\r\n").decode("utf-8", errors="replace")
            if not line:
                continue
            self._stderr_tail.append(line)

            # Stderr-side auth detection — same heuristic as Claude, kept
            # as a fallback even though the JSON-RPC error path is the
            # primary route for Codex.
            if not self._auth_emitted and any(
                p.lower() in line.lower() for p in _AUTH_FAIL_PATTERNS
            ):
                self._auth_emitted = True
                await self._on_event(
                    {
                        "type": "blemeesd.error",
                        "session_id": self.session_id,
                        "backend": self.backend,
                        "code": AUTH_FAILED,
                        "message": "Run `codex login` to re-authenticate.",
                    }
                )
                continue

            if self._stderr_limit.allow():
                await self._on_event(
                    {
                        "type": "blemeesd.stderr",
                        "session_id": self.session_id,
                        "line": line,
                    }
                )

    async def _watch_exit(self) -> None:
        assert self.proc is not None
        rc = await self.proc.wait()
        self._log.info("backend.exit", returncode=rc)
        if self._closing:
            return
        if self.turn_active or rc != 0:
            tail = " | ".join(self._stderr_tail) or f"exit {rc}"
            await self._on_event(
                {
                    "type": "blemeesd.error",
                    "session_id": self.session_id,
                    "backend": self.backend,
                    "code": BACKEND_CRASHED,
                    "message": f"stderr tail: {tail}"[:2048],
                }
            )
        self.turn_active = False


# ---------------------------------------------------------------------------
# RPC error helper
# ---------------------------------------------------------------------------


class _RpcError(Exception):
    """Internal: raised when a handshake JSON-RPC call returns an error."""

    def __init__(self, message: str, *, data: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.data = data


# ---------------------------------------------------------------------------
# Argv + tool-arg builders
# ---------------------------------------------------------------------------


def _serialise_config_value(value: Any) -> str:
    """Serialise a TOML-ish scalar / list / object for ``-c key=value``.

    Codex accepts both bare scalars and JSON for nested values. We pass
    JSON for non-string types so booleans and arrays round-trip cleanly.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def build_argv(codex_bin: str, *, options: dict[str, Any]) -> list[str]:
    """Build a ``codex mcp-server`` argv from ``options.codex.config``.

    `config` is forwarded as `-c key=value`. The optional
    `config.features` map becomes `--enable <name>` / `--disable <name>`.
    """
    argv: list[str] = [codex_bin, "mcp-server"]
    config = options.get("config")
    if not isinstance(config, dict):
        return argv
    for key, value in config.items():
        if key == "features":
            continue
        argv += ["-c", f"{key}={_serialise_config_value(value)}"]
    features = config.get("features")
    if isinstance(features, dict):
        for name, enabled in features.items():
            if not isinstance(name, str):
                continue
            if enabled:
                argv += ["--enable", name]
            else:
                argv += ["--disable", name]
    return argv


# Keys forwarded as `tools/call` arguments verbatim. (`config` is on the
# CLI; `include_raw_events` is a translation-layer flag.)
_TOOL_CALL_KEYS: frozenset[str] = frozenset(
    {
        "model",
        "profile",
        "cwd",
        "sandbox",
        "approval-policy",
        "base-instructions",
        "developer-instructions",
        "compact-prompt",
    }
)


def build_codex_tool_args(options: dict[str, Any], *, prompt: str) -> dict[str, Any]:
    """Assemble the `arguments` dict for the `codex` MCP tool call."""
    out: dict[str, Any] = {"prompt": prompt}
    for key in _TOOL_CALL_KEYS:
        if key in options and options[key] is not None:
            out[key] = options[key]
    return out


def _flatten_content_to_text(content: Any) -> str | None:
    """Reduce ``message.content`` to a single prompt string.

    Returns the flattened text when the input is a string or a list of
    text blocks; returns ``None`` if any block is non-text (forcing the
    backend to reject the turn with `invalid_message`).
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            return None
        if block.get("type") != "text":
            return None
        text = block.get("text", "")
        if not isinstance(text, str):
            return None
        parts.append(text)
    return "".join(parts)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


_VALID_OPTION_KEYS: frozenset[str] = frozenset(
    {
        "model",
        "profile",
        "cwd",
        "sandbox",
        "approval-policy",
        "base-instructions",
        "developer-instructions",
        "compact-prompt",
        "config",
        "include_raw_events",
    }
)


def validate_options(options: dict[str, Any]) -> None:
    """Reject unknown keys under `options.codex`. Mirrors the schema's
    `additionalProperties:false` so the runtime check survives even
    when the schema validator is bypassed.
    """
    for key in options:
        if key not in _VALID_OPTION_KEYS:
            from ..errors import ProtocolError

            raise ProtocolError(f"unknown options.codex field: {key!r}")


# ---------------------------------------------------------------------------
# On-disk transcript helpers
# ---------------------------------------------------------------------------


def codex_sessions_root() -> Path:
    """Return the root of Codex's rollout directory (``~/.codex/sessions``)."""
    return Path.home() / ".codex" / "sessions"


# Filename pattern: ``rollout-2026-04-27T14-42-22-<threadId>.jsonl``. The
# ``threadId`` is a UUID at the tail; the timestamp prefix is informative
# only.
_ROLLOUT_RE = re.compile(
    r"^rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-"
    r"(?P<thread_id>[0-9a-fA-F-]{8,})\.jsonl$"
)

# How far back we'll scan when listing rollouts. Codex's directory layout
# is YYYY/MM/DD; we walk ~30 newest day-dirs which is plenty for
# typical use without paying for a full scan of multi-year archives.
_LIST_MAX_DAYS = 30
# How many newest-first rollout files to consider per cwd before
# stopping. Caps worst-case I/O if a user has thousands of sessions.
_LIST_MAX_FILES = 256
# Lines to read from the head of a rollout when looking for the
# session_configured event (Codex emits it within the first few lines).
_ROLLOUT_HEAD_LINES = 16
# Cap on preview text length, mirrors the Claude backend.
_PREVIEW_CAP = 200


def _thread_id_from_filename(name: str) -> str | None:
    m = _ROLLOUT_RE.match(name)
    return m.group("thread_id") if m is not None else None


def _read_rollout_head(path: Path) -> list[dict[str, Any]]:
    """Best-effort: read up to the first ``_ROLLOUT_HEAD_LINES`` JSON
    lines of a rollout file. Non-JSON / malformed lines are skipped.
    """
    out: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for _ in range(_ROLLOUT_HEAD_LINES):
                line = fh.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
    except OSError:
        return out
    return out


def _extract_session_configured(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Find the embedded session_configured event in a rollout head.

    Codex stores rollout events with the same ``msg.{type,…}`` shape as
    the wire protocol, possibly nested inside a ``payload`` /
    ``params`` envelope. We accept either the flat-``msg`` form or a
    record whose ``type`` directly equals ``session_configured``.
    """
    for evt in events:
        if not isinstance(evt, dict):
            continue
        if evt.get("type") == "session_configured":
            return evt
        msg = evt.get("msg")
        if isinstance(msg, dict) and msg.get("type") == "session_configured":
            return msg
        payload = evt.get("payload")
        if isinstance(payload, dict) and payload.get("type") == "session_configured":
            return payload
    return None


def _first_user_preview_from_rollout(events: list[dict[str, Any]]) -> str | None:
    """Best-effort first-user-message preview (matching the Claude
    backend's behaviour). Looks for an ``UserMessage`` ``item_completed``
    or a flat ``user_message``.
    """
    for evt in events:
        if not isinstance(evt, dict):
            continue
        # Flat shape (matches the wire form).
        msg = evt.get("msg") if isinstance(evt.get("msg"), dict) else evt
        if not isinstance(msg, dict):
            continue
        # `item_completed{UserMessage}`.
        if msg.get("type") == "item_completed":
            item = msg.get("item")
            if isinstance(item, dict) and item.get("type") == "UserMessage":
                content = item.get("content")
                text = _content_text_blocks(content)
                if text is not None:
                    return text[:_PREVIEW_CAP]
        # `user_message{message}`.
        if msg.get("type") == "user_message":
            txt = msg.get("message")
            if isinstance(txt, str) and txt:
                return txt[:_PREVIEW_CAP]
    return None


def _content_text_blocks(content: Any) -> str | None:
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if isinstance(block_type, str) and block_type.lower() == "text":
            text = block.get("text", "")
            if isinstance(text, str):
                parts.append(text)
    if not parts:
        return None
    return "".join(parts)


def list_on_disk_sessions(cwd: str | None) -> list[dict]:
    """Enumerate Codex rollout files for ``cwd``.

    Walks ``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`` (newest day
    first), parses each rollout's embedded ``session_configured`` to
    filter by ``cwd``, and returns rows of the same shape the Claude
    backend emits: ``{session_id, mtime_ms, size, preview?, rollout_path}``.

    ``session_id`` is the Codex ``threadId`` parsed from the filename.
    """
    root = codex_sessions_root()
    if not root.is_dir():
        return []

    out: list[dict] = []
    days_scanned = 0
    files_considered = 0

    try:
        year_dirs = sorted(
            (d for d in root.iterdir() if d.is_dir() and d.name.isdigit()),
            key=lambda d: d.name,
            reverse=True,
        )
    except OSError:
        return out

    for year_dir in year_dirs:
        try:
            month_dirs = sorted(
                (d for d in year_dir.iterdir() if d.is_dir() and d.name.isdigit()),
                key=lambda d: d.name,
                reverse=True,
            )
        except OSError:
            continue
        for month_dir in month_dirs:
            try:
                day_dirs = sorted(
                    (d for d in month_dir.iterdir() if d.is_dir() and d.name.isdigit()),
                    key=lambda d: d.name,
                    reverse=True,
                )
            except OSError:
                continue
            for day_dir in day_dirs:
                days_scanned += 1
                if days_scanned > _LIST_MAX_DAYS:
                    break
                try:
                    entries = sorted(
                        (e for e in day_dir.iterdir() if e.is_file() and e.suffix == ".jsonl"),
                        key=lambda e: e.name,
                        reverse=True,
                    )
                except OSError:
                    continue
                for entry in entries:
                    files_considered += 1
                    if files_considered > _LIST_MAX_FILES:
                        return _sorted_rows(out)
                    thread_id = _thread_id_from_filename(entry.name)
                    if thread_id is None:
                        continue
                    head = _read_rollout_head(entry)
                    sc = _extract_session_configured(head)
                    if sc is None:
                        continue
                    rollout_cwd = sc.get("cwd") if isinstance(sc, dict) else None
                    if not isinstance(rollout_cwd, str) or rollout_cwd != cwd:
                        continue
                    try:
                        st = entry.stat()
                    except OSError:
                        continue
                    record: dict[str, Any] = {
                        "session_id": thread_id,
                        "mtime_ms": int(st.st_mtime * 1000),
                        "size": st.st_size,
                        "rollout_path": str(entry),
                    }
                    preview = _first_user_preview_from_rollout(head)
                    if preview is not None:
                        record["preview"] = preview
                    out.append(record)
            if days_scanned > _LIST_MAX_DAYS:
                break
        if days_scanned > _LIST_MAX_DAYS:
            break
    return _sorted_rows(out)


def _sorted_rows(rows: list[dict]) -> list[dict]:
    rows.sort(key=lambda r: r.get("mtime_ms") or 0, reverse=True)
    return rows


def session_file_path(_cwd: str | None, _session_id: str) -> Path | None:
    """Return ``None`` — Codex rollout paths are not derivable from
    ``cwd``/``session_id``. The daemon caches the path on
    :class:`Session` from ``session_configured`` and resolves it via
    :func:`blemees.session._session_transcript_path` when needed (e.g.
    ``close{delete:true}``).
    """
    return None


def find_session_by_id(thread_id: str) -> dict | None:
    """Locate a Codex rollout by ``threadId`` across all cwds.

    Walks ``~/.codex/sessions/YYYY/MM/DD/`` newest-first (capped at
    :data:`_LIST_MAX_DAYS` days, :data:`_LIST_MAX_FILES` files), matching
    filenames against the rollout regex and returning the first whose
    ``threadId`` equals *thread_id*. Reads the file head once to extract
    ``cwd`` / ``model`` from ``session_configured``.

    Returns ``{backend, session_id, native_session_id, cwd?, model?,
    mtime_ms, rollout_path}`` or ``None`` when nothing matches.
    """
    root = codex_sessions_root()
    if not root.is_dir():
        return None

    days_scanned = 0
    files_considered = 0
    try:
        year_dirs = sorted(
            (d for d in root.iterdir() if d.is_dir() and d.name.isdigit()),
            key=lambda d: d.name,
            reverse=True,
        )
    except OSError:
        return None

    for year_dir in year_dirs:
        try:
            month_dirs = sorted(
                (d for d in year_dir.iterdir() if d.is_dir() and d.name.isdigit()),
                key=lambda d: d.name,
                reverse=True,
            )
        except OSError:
            continue
        for month_dir in month_dirs:
            try:
                day_dirs = sorted(
                    (d for d in month_dir.iterdir() if d.is_dir() and d.name.isdigit()),
                    key=lambda d: d.name,
                    reverse=True,
                )
            except OSError:
                continue
            for day_dir in day_dirs:
                days_scanned += 1
                if days_scanned > _LIST_MAX_DAYS:
                    return None
                try:
                    entries = sorted(
                        (e for e in day_dir.iterdir() if e.is_file() and e.suffix == ".jsonl"),
                        key=lambda e: e.name,
                        reverse=True,
                    )
                except OSError:
                    continue
                for entry in entries:
                    files_considered += 1
                    if files_considered > _LIST_MAX_FILES:
                        return None
                    if _thread_id_from_filename(entry.name) != thread_id:
                        continue
                    try:
                        st = entry.stat()
                    except OSError:
                        continue
                    record: dict[str, Any] = {
                        "backend": "codex",
                        "session_id": thread_id,
                        "native_session_id": thread_id,
                        "mtime_ms": int(st.st_mtime * 1000),
                        "rollout_path": str(entry),
                    }
                    head = _read_rollout_head(entry)
                    sc = _extract_session_configured(head)
                    if isinstance(sc, dict):
                        if isinstance(sc.get("cwd"), str) and sc["cwd"]:
                            record["cwd"] = sc["cwd"]
                        if isinstance(sc.get("model"), str) and sc["model"]:
                            record["model"] = sc["model"]
                    return record
    return None


# ---------------------------------------------------------------------------
# Version detection
# ---------------------------------------------------------------------------


def detect_version(codex_bin: str) -> str | None:
    """Run ``codex --version``. Codex prints e.g. ``codex-cli 0.125.0``.

    Returns the version suffix (``0.125.0``) or the raw blob if the
    output doesn't match. Best-effort; ``None`` on failure.
    """
    path = shutil.which(codex_bin) or codex_bin
    try:
        out = _stdlib_subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, _stdlib_subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    blob = (out.stdout or out.stderr or "").strip()
    if not blob:
        return None
    parts = blob.split()
    if len(parts) >= 2:
        return parts[-1]
    m = _VERSION_RE.search(blob)
    return m.group(1) if m else blob


__all__ = [
    "CodexBackend",
    "build_argv",
    "build_codex_tool_args",
    "detect_version",
    "find_session_by_id",
    "list_on_disk_sessions",
    "session_file_path",
    "validate_options",
]
