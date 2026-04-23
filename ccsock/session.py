"""Session table for ccsockd (spec §3, §5.9, §6.5).

A :class:`Session` binds a session id to a :class:`ClaudeSubprocess`, the
connection that currently owns it (if any), and the original :class:`OpenMessage`
so we can respawn with ``--resume`` after an interrupt or reattach.

Sessions outlive client connections. When a client disconnects without
calling ``ccsockd.close``, the session is *detached*: the subprocess is
terminated (with 500 ms grace), but the session record is kept so another
connection can reattach with ``resume: true``. Detached sessions are reaped
after ``IDLE_TIMEOUT`` (default 900 s).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .errors import (
    SESSION_UNKNOWN,
    SessionExistsError,
    SessionUnknownError,
)
from .protocol import OpenMessage
from .subprocess import ClaudeSubprocess, session_file_path


@dataclass(slots=True)
class Session:
    session_id: str
    open_msg: OpenMessage
    cwd: str | None
    connection_id: Optional[int] = None
    subprocess: Optional[ClaudeSubprocess] = None
    detached_at: Optional[float] = None  # monotonic time at detach
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_attached(self) -> bool:
        return self.connection_id is not None


class SessionTable:
    """Central registry of open sessions."""

    def __init__(self, *, idle_timeout_s: float, max_concurrent: int) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = asyncio.Lock()
        self.idle_timeout_s = idle_timeout_s
        self.max_concurrent = max_concurrent

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    async def register(self, sess: Session) -> None:
        async with self._lock:
            if sess.session_id in self._sessions:
                raise SessionExistsError(sess.session_id)
            if len(self._sessions) >= self.max_concurrent:
                raise SessionExistsError(f"max_concurrent_sessions reached")
            self._sessions[sess.session_id] = sess

    async def attach_existing(self, session_id: str, connection_id: int) -> Session:
        async with self._lock:
            sess = self._sessions.get(session_id)
            if sess is None:
                raise SessionUnknownError(session_id)
            if sess.is_attached and sess.connection_id != connection_id:
                # Another connection owns it; detach first.
                sess.connection_id = connection_id
            else:
                sess.connection_id = connection_id
                sess.detached_at = None
            return sess

    def get(self, session_id: str) -> Session:
        sess = self._sessions.get(session_id)
        if sess is None:
            raise SessionUnknownError(session_id)
        return sess

    def try_get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def list_ids(self) -> list[str]:
        return list(self._sessions.keys())

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    async def detach(self, session_id: str) -> None:
        sess = self._sessions.get(session_id)
        if sess is None:
            return
        if sess.subprocess is not None:
            await sess.subprocess.close()
            sess.subprocess = None
        sess.connection_id = None
        sess.detached_at = time.monotonic()

    async def remove(self, session_id: str, *, delete_file: bool = False) -> None:
        async with self._lock:
            sess = self._sessions.pop(session_id, None)
        if sess is None:
            return
        if sess.subprocess is not None:
            await sess.subprocess.close()
        if delete_file:
            try:
                path = session_file_path(sess.cwd, sess.session_id)
                if path.is_file():
                    path.unlink()
            except OSError:
                pass

    async def detach_all_for_connection(self, connection_id: int) -> list[str]:
        """Detach every session owned by ``connection_id``; return the ids."""
        detached: list[str] = []
        # Snapshot first; detach modifies the dict entries in place.
        for sess in list(self._sessions.values()):
            if sess.connection_id == connection_id:
                await self.detach(sess.session_id)
                detached.append(sess.session_id)
        return detached

    # ------------------------------------------------------------------
    # Idle reaper
    # ------------------------------------------------------------------

    async def reap_idle(self, *, now: float | None = None) -> list[str]:
        """Remove detached sessions older than ``idle_timeout_s``."""
        if now is None:
            now = time.monotonic()
        expired: list[str] = []
        for sess in list(self._sessions.values()):
            if sess.connection_id is None and sess.detached_at is not None:
                if now - sess.detached_at >= self.idle_timeout_s:
                    expired.append(sess.session_id)
        for sid in expired:
            await self.remove(sid, delete_file=False)
        return expired

    async def shutdown(self) -> None:
        for sid in list(self._sessions.keys()):
            await self.remove(sid, delete_file=False)


def make_reaper(table: SessionTable, logger, *, interval_s: float = 30.0) -> asyncio.Task:
    """Start a background task that periodically prunes idle sessions."""

    async def _loop() -> None:
        while True:
            try:
                await asyncio.sleep(interval_s)
                expired = await table.reap_idle()
                if expired:
                    logger.info("session.reaped", count=len(expired), ids=expired)
            except asyncio.CancelledError:
                return
            except Exception:  # pragma: no cover - defensive
                logger.exception("session.reaper_error")

    return asyncio.create_task(_loop(), name="ccsock-reaper")
