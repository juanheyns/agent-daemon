"""Per-session agent backends.

Each backend wraps one upstream child process (`claude -p` or
`codex mcp-server`) and translates its native event stream into the
unified `agent.*` vocabulary documented in `docs/agent-events.md`. The
daemon dispatcher holds an `AgentBackend` instance per session and
drives it through this Protocol — it is intentionally narrow so the
daemon stays backend-agnostic.

Backends are responsible for:

* Spawning and respawning their child.
* Writing user turns in the child's native input shape.
* Reading the child's output and emitting one or more `agent.*` frames
  per native event via the per-session `on_event` callback (the same
  callback `Session.on_event` consumes).
* Cancelling an in-flight turn.
* Locating on-disk transcripts for `agent.list_sessions` and the
  optional retention sweep.
* Detecting auth failures in stderr / JSON-RPC errors so the daemon
  can surface `auth_failed`.

Concrete implementations live alongside this file:

* `blemees_agent.backends.claude.ClaudeBackend` — drives `claude -p`.
* `blemees_agent.backends.codex.CodexBackend`   — drives `codex mcp-server`.

Phase 1 ships the Claude backend; Phase 3 adds Codex.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

# The callback handed to a backend that takes one fully-translated
# `agent.*` (or daemon-synthesised `blemeesd.*`) frame and pushes it
# into the per-session event pipeline.
EventCallback = Callable[[dict[str, Any]], Awaitable[None]]

# Backend names that are valid on the wire. Mirrors the `Backend` $def
# in the JSON schemas. Kept as a frozenset so callers can do `in` checks
# in O(1) and the dispatcher can list registered backends without
# importing the concrete classes.
KNOWN_BACKENDS: frozenset[str] = frozenset({"claude", "codex"})


@runtime_checkable
class AgentBackend(Protocol):
    """Protocol every concrete backend implements.

    Implementations must be awaitable-friendly: every method is async
    *except* the property-shaped accessors and the file-system / argv
    helpers, which are pure.

    Event delivery uses the supplied `on_event` callback; the backend
    never touches `Session` directly. That keeps the dispatcher free
    to swap backends per session without leaking backend-specific
    plumbing into `session.py`.
    """

    backend: str
    """Backend name on the wire — one of `KNOWN_BACKENDS`."""

    pid: int | None
    """The child's PID once spawned, or `None` before/after."""

    turn_active: bool
    """True while a user turn is in flight (between user-write and
    `agent.result`)."""

    @property
    def running(self) -> bool:
        """True iff the child has been spawned and has not yet exited."""
        ...

    async def spawn(self) -> None:
        """Start the upstream child and begin consuming its output.

        Raises `SpawnFailedError` if the binary is missing or the OS
        refuses the launch.
        """
        ...

    async def send_user_turn(self, message: dict[str, Any]) -> None:
        """Deliver one user turn to the child.

        `message` is the validated payload from `agent.user.message`
        (`{"role":"user","content":...}`). The backend translates it
        into its native input shape (CC stream-json line; Codex
        `tools/call`).

        Raises `SessionBusyError` if a turn is already in flight, and
        `SpawnFailedError` if the underlying transport fails.
        """
        ...

    async def interrupt(self) -> bool:
        """Cancel the in-flight turn, if any.

        Returns `True` if a kill / cancel was issued, `False` if no
        turn was in flight (the caller emits
        `agent.interrupted{was_idle:true}` and skips any respawn).
        """
        ...

    async def close(self) -> None:
        """Tear down the child and release its readers."""
        ...

    async def wait_for_exit(self, timeout: float) -> bool:
        """Return `True` if the child has exited within `timeout` seconds."""
        ...


class BackendOnDiskListing(Protocol):
    """Class-level helpers a backend exposes for transcript discovery
    and retention sweeps. Kept separate from the per-session protocol
    so callers don't need a live backend instance to enumerate or
    delete transcripts.
    """

    @staticmethod
    def list_on_disk_sessions(cwd: str | None) -> list[dict[str, Any]]:
        """Return on-disk session summaries for `cwd`.

        Each row is `{session_id, mtime_ms, size, preview?}`,
        newest-first. Empty list if the backend doesn't store
        transcripts on disk or no transcripts exist for `cwd`.
        """
        ...

    @staticmethod
    def session_file_path(cwd: str | None, session_id: str) -> Path:
        """Return the expected transcript path for a session id."""
        ...


__all__ = [
    "AgentBackend",
    "BackendOnDiskListing",
    "EventCallback",
    "KNOWN_BACKENDS",
]
