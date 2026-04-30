"""Claude Code backend (`claude -p`).

Wraps a long-running `claude -p` child, drives it through CC's
stream-json stdio, and translates each native event into one or more
`agent.*` frames via :mod:`blemees.backends.translate_claude`.

Responsibilities:
    * Spawn / respawn (`--resume`) the child with a fixed argv template.
    * Feed `agent.user` turns to stdin in CC's stream-json input shape.
    * Parse stdout stream-json events; translate each to agent.* frames;
      enqueue to the per-session event pipeline.
    * Rate-limit stderr lines, detect auth-failure signatures, surface
      `backend_crashed` on non-zero exit mid-turn.
    * Interrupt: SIGTERM → 500 ms → SIGKILL, then respawn with `--resume`.

The wrapper is agnostic to the connection/session layer: it just needs
an `EventCallback` to push frames onto. That callback is the per-session
event pipeline described in spec §5.6 / §9.3.
"""

from __future__ import annotations

import asyncio
import json
import re
import signal
import time
import uuid
from collections import deque
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ..errors import (
    AUTH_FAILED,
    BACKEND_CRASHED,
    SessionBusyError,
    SpawnFailedError,
    UnsafeFlagError,
)
from . import EventCallback
from .translate_claude import TURN_END_TYPES, translate_event

# Signatures that indicate the CLI's OAuth token has expired. Spec §9.2.
_AUTH_FAIL_PATTERNS: tuple[str, ...] = (
    "401",
    "OAuth token expired",
    "Please run claude auth",
    "Session authentication failed",
)


class _StderrRateLimiter:
    """Rolling-window line limiter. Spec §5.6."""

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


class ClaudeBackend:
    """One child process running ``claude -p --output-format stream-json``."""

    backend = "claude"

    def __init__(
        self,
        *,
        session_id: str,
        argv: list[str],
        cwd: str | None,
        on_event: EventCallback,
        logger,
        options: dict[str, Any] | None = None,
        stderr_rate_lines: int = 50,
        stderr_rate_window_s: float = 10.0,
        include_raw_events: bool = False,
    ) -> None:
        self.session_id = session_id
        self._argv = argv
        self._cwd = cwd
        self._options = options or {}
        self._on_event = on_event
        self._log = logger.bind(session_id=session_id, backend=self.backend)
        self._stderr_limit = _StderrRateLimiter(stderr_rate_lines, stderr_rate_window_s)
        self._include_raw = include_raw_events

        self.proc: asyncio.subprocess.Process | None = None
        self.pid: int | None = None
        self.turn_active: bool = False
        self._closing: bool = False
        self._stderr_tail: deque[str] = deque(maxlen=20)
        self._auth_emitted: bool = False
        self._reader_tasks: list[asyncio.Task] = []
        self._stdin_lock = asyncio.Lock()

        # Per-turn state for daemon-side turn_id and TTFT measurement.
        # Codex gets these natively from `task_complete`; for parity we
        # measure them on the daemon side for Claude.
        self._current_turn_id: str | None = None
        self._turn_started_at_ms: int | None = None
        self._first_token_at_ms: int | None = None
        # Tracks whether the current turn has already been finalised by a
        # synthesised `agent.result` (e.g. mid-turn auth failure). Stops
        # `_watch_exit` from emitting a duplicate close on the same turn.
        self._turn_finalized: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def spawn(self) -> None:
        """Launch the child. Raises :class:`SpawnFailedError` on OS error."""
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
            raise SpawnFailedError(f"failed to launch claude: {exc}") from exc
        self.pid = self.proc.pid
        self._log = self._log.bind(pid=self.pid)
        self._reader_tasks = [
            asyncio.create_task(self._read_stdout(), name=f"claude-stdout-{self.session_id}"),
            asyncio.create_task(self._read_stderr(), name=f"claude-stderr-{self.session_id}"),
            asyncio.create_task(self._watch_exit(), name=f"claude-exit-{self.session_id}"),
        ]

    async def respawn_with_resume(self) -> None:
        """Replace ``--session-id X`` with ``--resume X`` and relaunch."""
        self._argv = argv_to_resume(self._argv, self.session_id)
        await self.spawn()

    # ------------------------------------------------------------------
    # Turn I/O
    # ------------------------------------------------------------------

    async def send_user_turn(self, message: dict[str, Any]) -> None:
        """Write one stream-json line on the child's stdin.

        Raises :class:`SessionBusyError` if a turn is already in flight.
        Allocates a per-turn ``turn_id`` and records the start timestamp so
        the synthesised metadata (``turn_id`` /
        ``time_to_first_token_ms``) on the eventual ``agent.result`` is
        symmetrical with what codex emits natively. Also emits a
        synthesised ``agent.notice{category:"task_started"}`` so clients
        get a turn-start hook on both backends.
        """
        if self.proc is None or self.proc.returncode is not None:
            raise SpawnFailedError("subprocess not running")
        if self.turn_active:
            raise SessionBusyError(self.session_id)
        line = build_user_stdin_line(session_id=self.session_id, message=message)
        async with self._stdin_lock:
            self.turn_active = True
            self._turn_finalized = False
            self._current_turn_id = uuid.uuid4().hex
            self._turn_started_at_ms = int(time.time() * 1000)
            self._first_token_at_ms = None
            assert self.proc.stdin is not None
            self.proc.stdin.write(line)
            try:
                await self.proc.stdin.drain()
            except (ConnectionResetError, BrokenPipeError) as exc:
                self.turn_active = False
                self._reset_turn_state()
                raise SpawnFailedError(f"stdin write failed: {exc}") from exc

        await self._on_event(
            {
                "type": "agent.notice",
                "level": "info",
                "category": "task_started",
                "data": {
                    "turn_id": self._current_turn_id,
                    "started_at_ms": self._turn_started_at_ms,
                },
                "backend": self.backend,
            }
        )

    # ------------------------------------------------------------------
    # Interrupt + close
    # ------------------------------------------------------------------

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

    async def interrupt(self) -> bool:
        """Terminate an in-flight turn and respawn via ``--resume``.

        Returns ``False`` if there was no in-flight turn (caller should emit
        ``blemeesd.interrupted`` with ``was_idle: true`` and skip the respawn).

        Also synthesises a closing ``agent.result{subtype:"interrupted"}`` so
        clients see a consistent turn lifecycle (spec §5.7). The codex
        backend gets this for free from its ``turn_aborted`` translator;
        for claude we have to emit it ourselves since the kill prevents
        the subprocess from producing a native ``result`` event. The
        emit is scheduled as a task so the caller can emit
        ``blemeesd.interrupted`` first — matching codex, where the
        async ``turn_aborted`` event arrives after the ack.
        """
        if not self.turn_active:
            return False
        # Capture turn metadata before respawn clears the per-turn state.
        synth = self._build_synth_result(subtype="interrupted")
        self._closing = True  # suppress the reader's crash report
        await self._kill()
        # Readers will unwind via _watch_exit; await their completion so that
        # the respawn starts cleanly.
        await self._drain_readers()
        self.turn_active = False
        self._turn_finalized = True
        self._reset_turn_state()
        self._closing = False
        await self.respawn_with_resume()
        asyncio.create_task(
            self._on_event(synth),
            name=f"claude-synth-interrupted-{self.session_id}",
        )
        return True

    async def close(self) -> None:
        self._closing = True
        await self._kill()
        await self._drain_readers()

    async def wait_for_exit(self, timeout: float) -> bool:
        """Return ``True`` if the subprocess has exited within ``timeout`` seconds.

        Used by the daemon's graceful-shutdown path to block on sessions that
        are finishing their current turn before force-killing stragglers.
        """
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
                # Line longer than buffer; skip to next newline.
                self._log.warning("backend.stdout_overrun")
                await stdout.read(1)
                continue
            if not raw:
                return
            line = raw.rstrip(b"\r\n")
            if not line:
                continue
            try:
                event = json.loads(line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                self._log.warning("backend.non_json_stdout", length=len(line))
                continue
            if not isinstance(event, dict):
                continue
            for frame in translate_event(event, include_raw=self._include_raw):
                # The translator returns frames without session_id / seq /
                # backend; the daemon's per-session event handler fills
                # session_id + seq, but we set `backend` here because it's
                # backend-specific.
                frame["backend"] = self.backend
                ftype = frame.get("type")
                if ftype == "agent.system_init":
                    if "native_session_id" not in frame:
                        # CC's wire doesn't carry `native_session_id`; it's
                        # the same as our session id (passed via `--session-id`).
                        frame["native_session_id"] = self.session_id
                    self._inject_capabilities(frame)
                elif ftype == "agent.delta" and self._first_token_at_ms is None:
                    # First delta of the turn — record for TTFT measurement.
                    self._first_token_at_ms = int(time.time() * 1000)
                if ftype in TURN_END_TYPES:
                    self._stamp_turn_metadata(frame)
                    self.turn_active = False
                    self._turn_finalized = True
                    self._reset_turn_state()
                await self._on_event(frame)

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

            # Auth-failure detection (§9.2); emitted at most once per spawn.
            if not self._auth_emitted and any(p in line for p in _AUTH_FAIL_PATTERNS):
                self._auth_emitted = True
                auth_msg = "Run `claude auth` to re-authenticate."
                await self._on_event(
                    {
                        "type": "blemeesd.error",
                        "session_id": self.session_id,
                        "backend": self.backend,
                        "code": AUTH_FAILED,
                        "message": auth_msg,
                    }
                )
                # Close the in-flight turn so clients waiting on
                # `agent.result` aren't left hanging. Mirrors the codex
                # backend's `_handle_turn_response → finalize_error` path.
                if self.turn_active and not self._turn_finalized:
                    synth = self._build_synth_result(
                        subtype="error",
                        error={"code": AUTH_FAILED, "message": auth_msg},
                    )
                    self.turn_active = False
                    self._turn_finalized = True
                    self._reset_turn_state()
                    await self._on_event(synth)
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
        # Crash mid-turn (or unexpected exit): surface to the client.
        if self.turn_active or rc != 0:
            tail = " | ".join(self._stderr_tail) or f"exit {rc}"
            crash_msg = f"stderr tail: {tail}"[:2048]
            await self._on_event(
                {
                    "type": "blemeesd.error",
                    "session_id": self.session_id,
                    "backend": self.backend,
                    "code": BACKEND_CRASHED,
                    "message": crash_msg,
                }
            )
            # Synthesise a closing `agent.result` so clients see a clean
            # turn end on backend crash. Spec §5.6 says result is always
            # the last frame for a turn — this restores that invariant
            # when the child exits before emitting its own `result`. The
            # `_turn_finalized` guard avoids a double close after
            # mid-turn auth-failure synthesis.
            if self.turn_active and not self._turn_finalized:
                synth = self._build_synth_result(
                    subtype="error",
                    error={"code": BACKEND_CRASHED, "message": crash_msg},
                )
                self._turn_finalized = True
                self._reset_turn_state()
                await self._on_event(synth)
        self.turn_active = False

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    # ------------------------------------------------------------------
    # Synthesis / per-turn metadata helpers
    # ------------------------------------------------------------------

    def _inject_capabilities(self, frame: dict[str, Any]) -> None:
        """Populate ``agent.system_init.capabilities`` for Claude.

        CC's wire-level ``system{init}`` event carries no capabilities
        block. We synthesise one from the open-time ``options.claude.*``
        so the frame has the same shape as Codex's
        ``session_configured`` translation. Field names mirror Codex
        where the concept overlaps (``reasoning_effort``, ``rollout_path``).
        """
        caps = dict(frame.get("capabilities") or {})
        opts = self._options
        permission_mode = opts.get("permission_mode")
        if isinstance(permission_mode, str):
            caps["permission_mode"] = permission_mode
        effort = opts.get("effort")
        if isinstance(effort, str):
            caps["reasoning_effort"] = effort
        try:
            rollout = session_file_path(self._cwd, self.session_id)
            caps["rollout_path"] = str(rollout)
        except Exception:  # pragma: no cover - defensive
            pass
        if caps:
            frame["capabilities"] = caps

    def _stamp_turn_metadata(self, frame: dict[str, Any]) -> None:
        """Attach ``turn_id`` + ``time_to_first_token_ms`` to a result-shaped frame.

        Mirrors the data Codex carries natively on its synthesised
        ``agent.result``. Daemon-side TTFT is wall-clock from
        ``send_user_turn`` to the first ``agent.delta``; ``turn_id`` is
        a per-turn UUID hex allocated in ``send_user_turn``.
        """
        if self._current_turn_id is not None:
            frame.setdefault("turn_id", self._current_turn_id)
        if self._first_token_at_ms is not None and self._turn_started_at_ms is not None:
            ttft = self._first_token_at_ms - self._turn_started_at_ms
            if ttft >= 0:
                frame.setdefault("time_to_first_token_ms", ttft)

    def _build_synth_result(
        self,
        *,
        subtype: str,
        error: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Construct a synthesised ``agent.result`` with current turn metadata."""
        synth: dict[str, Any] = {
            "type": "agent.result",
            "subtype": subtype,
            "num_turns": 1,
            "backend": self.backend,
        }
        if error is not None:
            synth["error"] = error
        self._stamp_turn_metadata(synth)
        return synth

    def _reset_turn_state(self) -> None:
        self._current_turn_id = None
        self._turn_started_at_ms = None
        self._first_token_at_ms = None


# ---------------------------------------------------------------------------
# Argv builders + safety filters
# ---------------------------------------------------------------------------


# Fields the daemon refuses to pass under `options.claude.*` (always
# rejected with `unsafe_flag`).
UNSAFE_OPTION_KEYS: frozenset[str] = frozenset(
    {
        "dangerously_skip_permissions",
        "allow_dangerously_skip_permissions",
        "bare",
        "continue",
        "continue_",
        "from_pr",
    }
)

# Daemon-owned CLI flags that clients must not set under any name
# (including via `options.claude.<>` aliases).
DAEMON_OWNED_OPTION_KEYS: frozenset[str] = frozenset({"input_format", "output_format"})

# Free-form values that, if they match a refused CLI flag literal, are
# rejected.
UNSAFE_LITERAL_FLAGS: frozenset[str] = frozenset(
    {
        "--dangerously-skip-permissions",
        "--allow-dangerously-skip-permissions",
        "--bare",
        "--continue",
        "--from-pr",
    }
)


# Whitelist of accepted `options.claude.*` keys. Each maps to one CC
# CLI flag in `build_argv` (a few are flags-with-no-arg, the rest take
# a value).
VALID_OPTION_KEYS: frozenset[str] = frozenset(
    {
        "model",
        "system_prompt",
        "append_system_prompt",
        "tools",
        "disallowed_tools",
        "permission_mode",
        "cwd",
        "add_dir",
        "effort",
        "agent",
        "agents",
        "mcp_config",
        "strict_mcp_config",
        "settings",
        "setting_sources",
        "plugin_dir",
        "betas",
        "exclude_dynamic_system_prompt_sections",
        "max_budget_usd",
        "json_schema",
        "fallback_model",
        "session_name",
        "session_persistence",
        "include_partial_messages",
        "replay_user_messages",
        # Translation-layer flag (not a CC CLI flag).
        "include_raw_events",
    }
)


def validate_options(options: dict[str, Any]) -> None:
    """Raise on unsafe / daemon-owned / unknown keys under options.claude.

    The wire schema rejects unknown keys via `additionalProperties:false`,
    but the daemon does its own pass too — the schema validator is
    optional in the dispatcher, and we want the same behaviour either
    way.
    """
    for key in options:
        if key in UNSAFE_OPTION_KEYS:
            raise UnsafeFlagError(key)
        if key in DAEMON_OWNED_OPTION_KEYS:
            from ..errors import ProtocolError

            raise ProtocolError(f"{key!r} is not client-settable; daemon always uses stream-json")
        if key not in VALID_OPTION_KEYS:
            from ..errors import ProtocolError

            raise ProtocolError(f"unknown options.claude field: {key!r}")
    for value in options.values():
        if isinstance(value, str) and value in UNSAFE_LITERAL_FLAGS:
            raise UnsafeFlagError(value)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str) and item in UNSAFE_LITERAL_FLAGS:
                    raise UnsafeFlagError(item)


def build_argv(
    claude_bin: str,
    *,
    session_id: str,
    options: dict[str, Any],
    for_resume: bool,
) -> list[str]:
    """Build a `claude -p` argv list from validated options.claude.*.

    The daemon always forces `--verbose --input-format stream-json
    --output-format stream-json` (required by the event multiplexer).

    Pass `for_resume=True` for both the resume-on-open path and the
    post-interrupt respawn — emits `--resume <session>`. Otherwise
    emits `--session-id <session>`.
    """
    argv: list[str] = [claude_bin, "-p", "--verbose"]

    if for_resume:
        argv += ["--resume", session_id]
    else:
        argv += ["--session-id", session_id]

    argv += ["--input-format", "stream-json"]
    argv += ["--output-format", "stream-json"]

    f = options

    def add(flag: str, value: Any) -> None:
        argv.append(flag)
        argv.append(str(value))

    def add_list(flag: str, values: Iterable[Any]) -> None:
        argv.append(flag)
        argv.extend(str(v) for v in values)

    if "model" in f and f["model"] is not None:
        add("--model", f["model"])
    if "system_prompt" in f and f["system_prompt"] is not None:
        add("--system-prompt", f["system_prompt"])
    if "append_system_prompt" in f and f["append_system_prompt"] is not None:
        add("--append-system-prompt", f["append_system_prompt"])
    if "tools" in f and f["tools"] is not None:
        # Empty string is a legitimate value (disable all tools).
        add("--tools", f["tools"])
    if "disallowed_tools" in f and f["disallowed_tools"]:
        add_list("--disallowedTools", f["disallowed_tools"])
    if "permission_mode" in f and f["permission_mode"] is not None:
        add("--permission-mode", f["permission_mode"])
    if "add_dir" in f and f["add_dir"]:
        add_list("--add-dir", f["add_dir"])
    if "effort" in f and f["effort"] is not None:
        add("--effort", f["effort"])
    if "agent" in f and f["agent"] is not None:
        add("--agent", f["agent"])
    if "agents" in f and f["agents"] is not None:
        if isinstance(f["agents"], str):
            add("--agents", f["agents"])
        else:
            add("--agents", json.dumps(f["agents"], separators=(",", ":")))
    if "mcp_config" in f and f["mcp_config"]:
        add_list("--mcp-config", f["mcp_config"])
    if f.get("strict_mcp_config"):
        argv.append("--strict-mcp-config")
    if "settings" in f and f["settings"] is not None:
        add("--settings", f["settings"])
    if "setting_sources" in f and f["setting_sources"] is not None:
        add("--setting-sources", f["setting_sources"])
    if "plugin_dir" in f and f["plugin_dir"]:
        for item in f["plugin_dir"]:
            add("--plugin-dir", item)
    if "betas" in f and f["betas"]:
        add_list("--betas", f["betas"])
    if f.get("exclude_dynamic_system_prompt_sections"):
        argv.append("--exclude-dynamic-system-prompt-sections")
    if "max_budget_usd" in f and f["max_budget_usd"] is not None:
        add("--max-budget-usd", f["max_budget_usd"])
    if "json_schema" in f and f["json_schema"] is not None:
        if isinstance(f["json_schema"], str):
            add("--json-schema", f["json_schema"])
        else:
            add("--json-schema", json.dumps(f["json_schema"], separators=(",", ":")))
    if "fallback_model" in f and f["fallback_model"] is not None:
        add("--fallback-model", f["fallback_model"])
    if "session_name" in f and f["session_name"] is not None:
        add("-n", f["session_name"])
    if "session_persistence" in f and f["session_persistence"] is False:
        argv.append("--no-session-persistence")
    if f.get("include_partial_messages"):
        argv.append("--include-partial-messages")
    if f.get("replay_user_messages"):
        argv.append("--replay-user-messages")

    return argv


def argv_to_resume(argv: list[str], session_id: str) -> list[str]:
    """Rewrite ``--session-id X`` → ``--resume X`` for respawn.

    If ``--resume X`` is already present the argv is returned unchanged.
    """
    out: list[str] = []
    i = 0
    replaced = False
    while i < len(argv):
        token = argv[i]
        if token == "--session-id" and i + 1 < len(argv):
            out.append("--resume")
            out.append(argv[i + 1])
            i += 2
            replaced = True
            continue
        out.append(token)
        i += 1
    if not replaced and "--resume" not in out:
        # Defensive: ensure resume flag is present.
        out += ["--resume", session_id]
    return out


def build_user_stdin_line(session_id: str, *, message: dict[str, Any]) -> bytes:
    """Wrap an `agent.user.message` into one stream-json line for CC stdin."""
    payload = {"type": "user", "message": message, "session_id": session_id}
    return (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# On-disk transcript helpers
# ---------------------------------------------------------------------------


def project_dir_for_cwd(cwd: str | None) -> Path:
    """Return the ``~/.claude/projects/<encoded-cwd>/`` directory CC uses.

    Mirrors Claude Code's path-encoding scheme: both ``/`` and ``_`` are
    replaced with ``-``, and the result is prefixed with a single
    leading ``-``. So ``/Users/me/test_dir`` lands at
    ``~/.claude/projects/-Users-me-test-dir/``. Used only for on-disk
    operations (delete-on-close, list-sessions); we don't round-trip
    decode (the encoding is lossy when paths legitimately contain
    dashes).
    """
    home = Path.home()
    raw = cwd or str(home)
    cwd_key = raw.replace("/", "-").replace("_", "-").lstrip("-")
    return home / ".claude" / "projects" / f"-{cwd_key}"


def session_file_path(cwd: str | None, session_id: str) -> Path:
    """Return the expected ``<project-dir>/<session>.jsonl`` path."""
    return project_dir_for_cwd(cwd) / f"{session_id}.jsonl"


_PREVIEW_CAP = 200
_PREVIEW_SCAN_LINES = 8


def _first_user_preview(path: Path) -> str | None:
    """Best-effort extraction of the first user message from a CC transcript."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for _ in range(_PREVIEW_SCAN_LINES):
                line = fh.readline()
                if not line:
                    break
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(evt, dict) or evt.get("type") != "user":
                    continue
                msg = evt.get("message") or {}
                content = msg.get("content")
                text: str | None = None
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = block.get("text", "")
                            break
                if text is None:
                    continue
                return text[:_PREVIEW_CAP]
    except OSError:
        return None
    return None


def list_on_disk_sessions(cwd: str | None) -> list[dict]:
    """Enumerate on-disk CC transcripts for ``cwd``.

    Returns newest-first summaries: ``{session_id, mtime_ms, size, preview?}``.
    Returns an empty list if the project directory does not exist.
    """
    project_dir = project_dir_for_cwd(cwd)
    out: list[dict] = []
    try:
        entries = list(project_dir.iterdir())
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        return out
    for entry in entries:
        if entry.suffix != ".jsonl" or not entry.is_file():
            continue
        try:
            st = entry.stat()
        except OSError:
            continue
        record: dict = {
            "session_id": entry.stem,
            "mtime_ms": int(st.st_mtime * 1000),
            "size": st.st_size,
        }
        preview = _first_user_preview(entry)
        if preview is not None:
            record["preview"] = preview
        out.append(record)
    out.sort(key=lambda r: r["mtime_ms"], reverse=True)
    return out


def _scan_transcript_metadata(path: Path) -> dict[str, Any]:
    """Read the first lines of a CC transcript for ``cwd`` / ``model``.

    Real CC transcripts carry ``cwd`` at the top level on most records
    but nest ``model`` under ``event.message.model`` (assistant events).
    We scan up to a handful of lines and check both shapes; returns
    ``{}`` on read failure.
    """
    out: dict[str, Any] = {}
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for _ in range(_PREVIEW_SCAN_LINES):
                line = fh.readline()
                if not line:
                    break
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(evt, dict):
                    continue
                if "cwd" not in out and isinstance(evt.get("cwd"), str) and evt["cwd"]:
                    out["cwd"] = evt["cwd"]
                if "model" not in out:
                    top = evt.get("model")
                    msg = evt.get("message") if isinstance(evt.get("message"), dict) else None
                    nested = msg.get("model") if msg is not None else None
                    for candidate in (top, nested):
                        if isinstance(candidate, str) and candidate:
                            out["model"] = candidate
                            break
                if "cwd" in out and "model" in out:
                    break
    except OSError:
        pass
    return out


def find_session_by_id(session_id: str) -> dict | None:
    """Locate a CC transcript by session_id across all known projects.

    Walks ``~/.claude/projects/*/<session_id>.jsonl``. Returns
    ``{backend, session_id, native_session_id, cwd?, model?, mtime_ms,
    rollout_path}`` for the newest matching transcript, or ``None`` if
    no match. ``cwd`` / ``model`` are extracted from the transcript
    head (which carries them inline on most event records); both are
    omitted from the result when not derivable.
    """
    projects_root = Path.home() / ".claude" / "projects"
    if not projects_root.is_dir():
        return None
    best: tuple[float, Path] | None = None
    try:
        for project_dir in projects_root.iterdir():
            if not project_dir.is_dir():
                continue
            candidate = project_dir / f"{session_id}.jsonl"
            if not candidate.is_file():
                continue
            try:
                st = candidate.stat()
            except OSError:
                continue
            if best is None or st.st_mtime > best[0]:
                best = (st.st_mtime, candidate)
    except OSError:
        return None
    if best is None:
        return None
    mtime, path = best
    record: dict[str, Any] = {
        "backend": "claude",
        "session_id": session_id,
        "native_session_id": session_id,
        "mtime_ms": int(mtime * 1000),
        "rollout_path": str(path),
    }
    record.update(_scan_transcript_metadata(path))
    return record


# ---------------------------------------------------------------------------
# Version detection
# ---------------------------------------------------------------------------


_VERSION_RE = re.compile(r"\b(\d+(?:\.\d+){1,3}\S*)")


def detect_version(claude_bin: str) -> str | None:
    """Run ``claude --version`` once at startup. Best-effort; None on failure."""
    import shutil
    import subprocess

    path = shutil.which(claude_bin) or claude_bin
    try:
        out = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    blob = (out.stdout or out.stderr or "").strip()
    if not blob:
        return None
    m = _VERSION_RE.search(blob)
    return m.group(1) if m else blob


__all__ = [
    "ClaudeBackend",
    "argv_to_resume",
    "build_argv",
    "build_user_stdin_line",
    "detect_version",
    "find_session_by_id",
    "list_on_disk_sessions",
    "project_dir_for_cwd",
    "session_file_path",
    "validate_options",
    "VALID_OPTION_KEYS",
    "UNSAFE_OPTION_KEYS",
    "DAEMON_OWNED_OPTION_KEYS",
]
