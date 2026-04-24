"""Session table for blemeesd (spec §3, §5.9, §6.5).

A :class:`Session` binds a session id to a :class:`ClaudeSubprocess`, the
connection that currently owns it (if any), and the original :class:`OpenMessage`
so we can respawn with ``--resume`` after an interrupt or reattach.

Sessions outlive client connections. When a client disconnects without
calling ``blemeesd.close``, the session is *detached*: if a turn is in
flight the subprocess is allowed to run to the next ``result`` event (so
the transcript closes cleanly), otherwise it is terminated immediately.
Either way, the session record is kept so another connection can
reattach with ``resume: true`` and optionally replay missed events via
``last_seen_seq``. Detached sessions are reaped after ``IDLE_TIMEOUT``
(default 900 s).

Each outbound frame carries a monotonic ``seq`` assigned by the session.
Recent frames are kept in an in-memory :class:`RingBuffer` and
(optionally) persisted to a :class:`DurableEventLog` so clients can
reconnect and catch up across disconnects or daemon restarts.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from .errors import SessionExistsError, SessionUnknownError
from .event_log import DurableEventLog, RingBuffer, event_log_path
from .protocol import OpenMessage
from .subprocess import ClaudeSubprocess, session_file_path


WriterFn = Callable[[dict], Awaitable[None]]


@dataclass(slots=True)
class Session:
    session_id: str
    open_msg: OpenMessage
    cwd: str | None
    connection_id: Optional[int] = None
    subprocess: Optional[ClaudeSubprocess] = None
    detached_at: Optional[float] = None

    # Event-stream state -------------------------------------------------
    seq: int = 0
    ring: RingBuffer = field(default_factory=lambda: RingBuffer(1024))
    log: Optional[DurableEventLog] = None
    _writer: Optional[WriterFn] = None
    _finishing: bool = False  # subprocess keeps running, kill on next result

    extra: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Event dispatch
    # ------------------------------------------------------------------

    async def on_event(self, frame: dict) -> None:
        """Called by :class:`ClaudeSubprocess` for every event it emits.

        Tags with ``seq``, appends to the ring + durable log, and pushes
        to the attached writer (if any). If we're in ``finishing`` mode
        and this is the turn-ending ``result``, schedule a clean kill.
        """
        self.seq += 1
        frame["seq"] = self.seq
        self.ring.append(frame)
        if self.log is not None:
            try:
                self.log.append(frame)
            except OSError:
                pass  # disk issue; the ring still has it.
        writer = self._writer
        if writer is not None:
            try:
                await writer(frame)
            except Exception:
                # The writer's dead; treat as a silent detach. The session
                # stays live so a future attach can still replay.
                self._writer = None
        # Soft-kill after a completed turn when the client has left. We
        # match on the namespaced form emitted by the subprocess reader.
        if self._finishing and frame.get("type") == "claude.result":
            self._finishing = False
            sub = self.subprocess
            if sub is not None:
                asyncio.create_task(
                    sub.close(), name=f"cc-soft-kill-{self.session_id}"
                )

    # ------------------------------------------------------------------
    # Attach / detach
    # ------------------------------------------------------------------

    async def attach(
        self,
        connection_id: int,
        writer: WriterFn,
        *,
        last_seen_seq: int | None = None,
    ) -> dict:
        """Take ownership, replay any missed events, then stream live.

        Returns a small summary ``{replayed, gap_from, gap_to}`` for logging.
        """
        self.connection_id = connection_id
        self.detached_at = None
        self._writer = writer

        summary = {"replayed": 0, "gap_from": 0, "gap_to": 0}
        if last_seen_seq is None:
            return summary

        to_replay = self.ring.since(last_seen_seq)
        earliest = self.ring.earliest_seq()
        if earliest is not None and earliest > last_seen_seq + 1:
            # We dropped frames with seq in (last_seen_seq, earliest).
            await writer(
                {
                    "type": "blemeesd.replay_gap",
                    "session": self.session_id,
                    "since_seq": last_seen_seq,
                    "first_available_seq": earliest,
                    "seq": None,  # informational, not part of the seq stream
                }
            )
            summary["gap_from"] = last_seen_seq + 1
            summary["gap_to"] = earliest - 1
        elif not to_replay and self.seq > last_seen_seq:
            await writer(
                {
                    "type": "blemeesd.replay_gap",
                    "session": self.session_id,
                    "since_seq": last_seen_seq,
                    "first_available_seq": self.seq + 1,
                    "seq": None,
                }
            )
            summary["gap_from"] = last_seen_seq + 1
            summary["gap_to"] = self.seq

        for frame in to_replay:
            await writer(frame)
        summary["replayed"] = len(to_replay)
        return summary

    def detach_writer(self) -> None:
        """Unhook the writer without touching the subprocess."""
        self._writer = None
        self.connection_id = None
        self.detached_at = time.monotonic()

    def mark_finishing(self) -> None:
        """Ask the subprocess to self-kill after the current turn ends."""
        self._finishing = True

    # ------------------------------------------------------------------
    # Log setup
    # ------------------------------------------------------------------

    def enable_durable_log(self, base_dir: Path | str) -> None:
        """Attach a durable log; seed the ring from the tail of any prior file."""
        path = event_log_path(base_dir, self.session_id)
        log = DurableEventLog(path)
        # Seed from tail so we can replay across a daemon restart.
        try:
            prior = log.tail(self.ring.capacity)
        except OSError:
            prior = []
        if prior:
            self.ring.extend(prior)
            latest = self.ring.latest_seq() or 0
            if latest > self.seq:
                self.seq = latest
        log.open()
        self.log = log


# ---------------------------------------------------------------------------
# SessionTable
# ---------------------------------------------------------------------------


class SessionTable:
    """Central registry of open sessions."""

    def __init__(
        self,
        *,
        idle_timeout_s: float,
        max_concurrent: int,
        ring_buffer_size: int = 1024,
        event_log_dir: str | None = None,
    ) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = asyncio.Lock()
        self.idle_timeout_s = idle_timeout_s
        self.max_concurrent = max_concurrent
        self.ring_buffer_size = ring_buffer_size
        self.event_log_dir = event_log_dir

    # ------------------------------------------------------------------

    def new_session(self, open_msg: OpenMessage) -> Session:
        """Instantiate (but don't register) a ``Session`` with table defaults."""
        sess = Session(
            session_id=open_msg.session,
            open_msg=open_msg,
            cwd=open_msg.fields.get("cwd"),
            ring=RingBuffer(self.ring_buffer_size),
        )
        if self.event_log_dir:
            sess.enable_durable_log(self.event_log_dir)
        return sess

    async def register(self, sess: Session) -> None:
        async with self._lock:
            if sess.session_id in self._sessions:
                raise SessionExistsError(sess.session_id)
            if len(self._sessions) >= self.max_concurrent:
                raise SessionExistsError("max_concurrent_sessions reached")
            self._sessions[sess.session_id] = sess

    def get(self, session_id: str) -> Session:
        sess = self._sessions.get(session_id)
        if sess is None:
            raise SessionUnknownError(session_id)
        return sess

    def try_get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def list_ids(self) -> list[str]:
        return list(self._sessions.keys())

    def iter_by_cwd(self, cwd: str | None) -> list[Session]:
        return [s for s in self._sessions.values() if s.cwd == cwd]

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    async def detach_soft(self, session_id: str) -> None:
        """Unhook the writer. If a turn is in flight, let it finish before kill."""
        sess = self._sessions.get(session_id)
        if sess is None:
            return
        sess.detach_writer()
        sub = sess.subprocess
        if sub is None or not sub.running:
            sess.subprocess = None
            return
        if sub.turn_active:
            sess.mark_finishing()
        else:
            await sub.close()
            sess.subprocess = None

    async def detach_all_for_connection(self, connection_id: int) -> list[str]:
        detached: list[str] = []
        for sess in list(self._sessions.values()):
            if sess.connection_id == connection_id:
                await self.detach_soft(sess.session_id)
                detached.append(sess.session_id)
        return detached

    async def remove(self, session_id: str, *, delete_file: bool = False) -> None:
        async with self._lock:
            sess = self._sessions.pop(session_id, None)
        if sess is None:
            return
        if sess.subprocess is not None:
            await sess.subprocess.close()
        if sess.log is not None:
            if delete_file:
                sess.log.unlink()
            else:
                sess.log.close()
        if delete_file:
            try:
                path = session_file_path(sess.cwd, sess.session_id)
                if path.is_file():
                    path.unlink()
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Idle reaper
    # ------------------------------------------------------------------

    async def reap_idle(self, *, now: float | None = None) -> list[str]:
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

    return asyncio.create_task(_loop(), name="blemees-reaper")
