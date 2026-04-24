"""Wrapper around a long-running ``claude -p`` child (spec §6).

Responsibilities:
    * Spawn and respawn (``--resume``) the child with a fixed argv template.
    * Feed ``claude.user`` turns to stdin.
    * Parse stdout stream-json events; inject ``"session_id"`` and enqueue to the
      connection event queue.
    * Rate-limit stderr lines, detect OAuth-expiry signatures, surface
      ``claude_crashed`` on non-zero exit mid-turn.
    * Interrupt: SIGTERM → 500 ms → SIGKILL, then respawn with ``--resume``.

The wrapper is agnostic to the connection/session layer: it just needs an
``asyncio.Queue`` to push frames onto. That queue is the per-connection
bounded event queue described in §9.3.
"""

from __future__ import annotations

import asyncio
import json
import signal
import time
from collections import deque
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from .errors import (
    CLAUDE_CRASHED,
    OAUTH_EXPIRED,
    SessionBusyError,
    SpawnFailedError,
)

# Signatures that indicate the CLI's OAuth token has expired. Spec §9.2.
_OAUTH_PATTERNS = (
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


class ClaudeSubprocess:
    """One child process running ``claude -p --output-format stream-json``."""

    def __init__(
        self,
        *,
        session_id: str,
        argv: list[str],
        cwd: str | None,
        on_event: Callable[[dict], Awaitable[None]],
        logger,
        stderr_rate_lines: int = 50,
        stderr_rate_window_s: float = 10.0,
        on_exit: Callable[[ClaudeSubprocess], Awaitable[None]] | None = None,
    ) -> None:
        self.session_id = session_id
        self._argv = argv
        self._cwd = cwd
        self._on_event = on_event
        self._log = logger.bind(session_id=session_id)
        self._stderr_limit = _StderrRateLimiter(stderr_rate_lines, stderr_rate_window_s)
        self._on_exit = on_exit

        self.proc: asyncio.subprocess.Process | None = None
        self.pid: int | None = None
        self.turn_active: bool = False
        self._closing: bool = False
        self._stderr_tail: deque[str] = deque(maxlen=20)
        self._oauth_emitted: bool = False
        self._reader_tasks: list[asyncio.Task] = []
        self._stdin_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def spawn(self) -> None:
        """Launch the child. Raises :class:`SpawnFailedError` on OS error."""
        self._log.info(
            "subprocess.spawn",
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
            asyncio.create_task(self._read_stdout(), name=f"cc-stdout-{self.session_id}"),
            asyncio.create_task(self._read_stderr(), name=f"cc-stderr-{self.session_id}"),
            asyncio.create_task(self._watch_exit(), name=f"cc-exit-{self.session_id}"),
        ]

    async def respawn_with_resume(self) -> None:
        """Replace ``--session-id X`` with ``--resume X`` and relaunch."""
        self._argv = _argv_to_resume(self._argv, self.session_id)
        await self.spawn()

    # ------------------------------------------------------------------
    # Turn I/O
    # ------------------------------------------------------------------

    async def send_user_line(self, line: bytes) -> None:
        """Write a single stream-json input line to stdin.

        Raises :class:`SessionBusyError` if a turn is already in flight.
        """
        if self.proc is None or self.proc.returncode is not None:
            raise SpawnFailedError("subprocess not running")
        if self.turn_active:
            raise SessionBusyError(self.session_id)
        async with self._stdin_lock:
            self.turn_active = True
            assert self.proc.stdin is not None
            self.proc.stdin.write(line)
            try:
                await self.proc.stdin.drain()
            except (ConnectionResetError, BrokenPipeError) as exc:
                self.turn_active = False
                raise SpawnFailedError(f"stdin write failed: {exc}") from exc

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
                self._log.error("subprocess.kill_failed")

    async def interrupt(self) -> bool:
        """Terminate an in-flight turn and respawn via ``--resume``.

        Returns ``False`` if there was no in-flight turn (caller should emit
        ``blemeesd.interrupted`` with ``was_idle: true`` and skip the respawn).
        """
        if not self.turn_active:
            return False
        self._closing = True  # suppress the reader's crash report
        await self._kill()
        # Readers will unwind via _watch_exit; await their completion so that
        # the respawn starts cleanly.
        await self._drain_readers()
        self.turn_active = False
        self._closing = False
        await self.respawn_with_resume()
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
                self._log.warning("subprocess.stdout_overrun")
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
                self._log.warning("subprocess.non_json_stdout", length=len(line))
                continue
            if not isinstance(event, dict):
                continue
            event["session_id"] = self.session_id
            # Detect turn-end against the native CC type *before* namespacing
            # it, so the check stays stable if we ever change the prefix.
            orig_type = event.get("type")
            if orig_type == "result":
                self.turn_active = False
            # Namespace CC native events under ``claude.*`` so clients can
            # disambiguate them from ``blemeesd.*`` daemon frames without
            # ambiguity. Daemon-originated frames (e.g. the subprocess's own
            # ``blemeesd.stderr`` / ``blemeesd.error``) are emitted with their
            # prefix already set and are left alone.
            if isinstance(orig_type, str) and not orig_type.startswith(("blemeesd.", "claude.")):
                event["type"] = f"claude.{orig_type}"
            await self._enqueue(event)

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

            # OAuth-expired detection (§9.2); emitted at most once per spawn.
            if not self._oauth_emitted and any(p in line for p in _OAUTH_PATTERNS):
                self._oauth_emitted = True
                await self._enqueue(
                    {
                        "type": "blemeesd.error",
                        "session_id": self.session_id,
                        "code": OAUTH_EXPIRED,
                        "message": "Run `claude auth` to re-authenticate.",
                    }
                )
                continue

            if self._stderr_limit.allow():
                await self._enqueue(
                    {
                        "type": "blemeesd.stderr",
                        "session_id": self.session_id,
                        "line": line,
                    }
                )

    async def _watch_exit(self) -> None:
        assert self.proc is not None
        rc = await self.proc.wait()
        self._log.info("subprocess.exit", returncode=rc)
        if self._closing:
            return
        # Crash mid-turn (or unexpected exit): surface to the client.
        if self.turn_active or rc != 0:
            tail = " | ".join(self._stderr_tail) or f"exit {rc}"
            await self._enqueue(
                {
                    "type": "blemeesd.error",
                    "session_id": self.session_id,
                    "code": CLAUDE_CRASHED,
                    "message": f"stderr tail: {tail}"[:2048],
                }
            )
        self.turn_active = False
        if self._on_exit is not None:
            try:
                await self._on_exit(self)
            except Exception:  # pragma: no cover - defensive
                self._log.exception("subprocess.on_exit_failed")

    # ------------------------------------------------------------------

    async def _enqueue(self, frame: dict[str, Any]) -> None:
        # The Session is the authoritative event dispatcher: it tags with a
        # monotonic seq, ring-buffers, (optionally) writes to the durable
        # log, and pushes to whatever writer is currently attached.
        await self._on_event(frame)

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None


# ---------------------------------------------------------------------------
# Argv helpers
# ---------------------------------------------------------------------------


def _argv_to_resume(argv: list[str], session_id: str) -> list[str]:
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


def project_dir_for_cwd(cwd: str | None) -> Path:
    """Return the ``~/.claude/projects/<encoded-cwd>/`` directory CC uses.

    Mirrors Claude Code's "slashes → dashes, leading dash" encoding. We
    don't rely on this for correctness within a session; it's only used
    for on-disk operations (delete-on-close, list-sessions).
    """
    home = Path.home()
    cwd_key = (cwd or str(home)).replace("/", "-").lstrip("-")
    return home / ".claude" / "projects" / f"-{cwd_key}"


def session_file_path(cwd: str | None, session_id: str) -> Path:
    """Return the expected ``<project-dir>/<session>.jsonl`` path."""
    return project_dir_for_cwd(cwd) / f"{session_id}.jsonl"


_PREVIEW_CAP = 200
_PREVIEW_SCAN_LINES = 8


def _first_user_preview(path: Path) -> str | None:
    """Best-effort extraction of the first user message from a CC transcript.

    Scans the first few lines, returning the text of the first ``type:"user"``
    record (content may be a string or a list of blocks). Returns ``None`` if
    we can't find one — treat as "no preview available".
    """
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


def list_session_files(cwd: str | None) -> list[dict]:
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
