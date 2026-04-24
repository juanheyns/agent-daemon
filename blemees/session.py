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
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
    connection_id: int | None = None
    subprocess: ClaudeSubprocess | None = None
    detached_at: float | None = None

    # Event-stream state -------------------------------------------------
    seq: int = 0
    ring: RingBuffer = field(default_factory=lambda: RingBuffer(1024))
    log: DurableEventLog | None = None
    _writer: WriterFn | None = None
    _finishing: bool = False  # subprocess keeps running, kill on next result

    # Non-driving subscribers: connection_id → writer. Watchers receive every
    # frame the primary writer gets (claude.* events, blemeesd.stderr,
    # blemeesd.error{claude_crashed,oauth_expired}, and replays on subscribe)
    # but cannot drive the session (user/interrupt/close).
    _watchers: dict[int, WriterFn] = field(default_factory=dict)

    # Running usage accumulator maintained from claude.result frames. Persists
    # to <event_log_dir>/<session>.usage.json so it survives daemon restarts
    # whenever the durable log is enabled.
    turns: int = 0
    last_model: str | None = None
    last_turn_at_ms: int | None = None
    last_turn_usage: dict[str, int] = field(default_factory=dict)
    cumulative_usage: dict[str, int] = field(
        default_factory=lambda: {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
    )
    _usage_path: Path | None = None

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
        # Maintain usage / turn counters from CC native events.
        self._update_usage_from_frame(frame)
        writer = self._writer
        if writer is not None:
            try:
                await writer(frame)
            except Exception:
                # The writer's dead; treat as a silent detach. The session
                # stays live so a future attach can still replay.
                self._writer = None
        # Fan out to watchers. A failed watcher is silently dropped.
        if self._watchers:
            dead: list[int] = []
            for conn_id, w in self._watchers.items():
                try:
                    await w(frame)
                except Exception:
                    dead.append(conn_id)
            for conn_id in dead:
                self._watchers.pop(conn_id, None)
        # Soft-kill after a completed turn when the client has left. We
        # match on the namespaced form emitted by the subprocess reader.
        if self._finishing and frame.get("type") == "claude.result":
            self._finishing = False
            sub = self.subprocess
            if sub is not None:
                asyncio.create_task(sub.close(), name=f"cc-soft-kill-{self.session_id}")

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

        # Replay available frames first so the client sees them before the gap notice.
        for frame in to_replay:
            await writer(frame)
        summary["replayed"] = len(to_replay)

        # After replaying, notify about any gap with a properly sequenced frame.
        if earliest is not None and earliest > last_seen_seq + 1:
            # Frames with seq in (last_seen_seq, earliest) were dropped from the ring.
            self.seq += 1
            await writer(
                {
                    "type": "blemeesd.replay_gap",
                    "session_id": self.session_id,
                    "since_seq": last_seen_seq,
                    "first_available_seq": earliest,
                    "seq": self.seq,
                }
            )
            summary["gap_from"] = last_seen_seq + 1
            summary["gap_to"] = earliest - 1
        elif not to_replay and self.seq > last_seen_seq:
            # Ring has rolled past last_seen_seq with nothing to replay.
            old_seq = self.seq
            self.seq += 1
            await writer(
                {
                    "type": "blemeesd.replay_gap",
                    "session_id": self.session_id,
                    "since_seq": last_seen_seq,
                    "first_available_seq": self.seq + 1,
                    "seq": self.seq,
                }
            )
            summary["gap_from"] = last_seen_seq + 1
            summary["gap_to"] = old_seq
        return summary

    def detach_writer(self) -> None:
        """Unhook the writer without touching the subprocess."""
        self._writer = None
        self.connection_id = None
        self.detached_at = time.monotonic()

    def mark_finishing(self) -> None:
        """Ask the subprocess to self-kill after the current turn ends."""
        self._finishing = True

    async def add_watcher(
        self,
        connection_id: int,
        writer: WriterFn,
        *,
        last_seen_seq: int | None = None,
    ) -> dict:
        """Subscribe a non-driving writer. Replays missed frames if asked.

        Returns a ``{replayed, gap_from, gap_to}`` summary, same shape as
        :meth:`attach`.
        """
        # If this connection was already watching (e.g. re-watch), refresh.
        self._watchers[connection_id] = writer
        summary = {"replayed": 0, "gap_from": 0, "gap_to": 0}
        if last_seen_seq is None:
            return summary
        to_replay = self.ring.since(last_seen_seq)
        earliest = self.ring.earliest_seq()
        # Replay available frames first, then notify about any gap.
        for frame in to_replay:
            await writer(frame)
        summary["replayed"] = len(to_replay)
        if earliest is not None and earliest > last_seen_seq + 1:
            self.seq += 1
            await writer(
                {
                    "type": "blemeesd.replay_gap",
                    "session_id": self.session_id,
                    "since_seq": last_seen_seq,
                    "first_available_seq": earliest,
                    "seq": self.seq,
                }
            )
            summary["gap_from"] = last_seen_seq + 1
            summary["gap_to"] = earliest - 1
        return summary

    def remove_watcher(self, connection_id: int) -> bool:
        """Unsubscribe a watcher. Returns ``True`` if it was registered."""
        return self._watchers.pop(connection_id, None) is not None

    # ------------------------------------------------------------------
    # Log setup
    # ------------------------------------------------------------------

    def enable_durable_log(self, base_dir: Path | str) -> None:
        """Attach a durable log; seed the ring from the tail of any prior file.

        Also loads a ``<session>.usage.json`` sidecar next to the log so
        the cumulative usage accumulator survives daemon restarts.
        """
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
        self._usage_path = Path(base_dir) / f"{self.session_id}.usage.json"
        self._load_usage_sidecar()

    # ------------------------------------------------------------------
    # Usage accumulator
    # ------------------------------------------------------------------

    def _update_usage_from_frame(self, frame: dict) -> None:
        """Pull model / usage out of relevant CC events. Persist if enabled."""
        t = frame.get("type")
        if t == "claude.system" and frame.get("subtype") == "init":
            model = frame.get("model")
            if isinstance(model, str):
                self.last_model = model
            return
        if t != "claude.result":
            return
        usage = frame.get("usage")
        if not isinstance(usage, dict):
            return
        # Snapshot last-turn usage verbatim so clients see whatever fields CC
        # emits (including future ones we don't know about yet).
        self.last_turn_usage = dict(usage)
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        ):
            self.cumulative_usage[key] = self.cumulative_usage.get(key, 0) + int(
                usage.get(key, 0) or 0
            )
        self.turns += 1
        self.last_turn_at_ms = int(time.time() * 1000)
        self._save_usage_sidecar()

    def usage_snapshot(self, *, attached: bool, subprocess_running: bool) -> dict:
        """Build the payload for a ``blemeesd.session_info_reply`` frame."""
        last_inputs = (
            int(self.last_turn_usage.get("input_tokens", 0) or 0)
            + int(self.last_turn_usage.get("cache_read_input_tokens", 0) or 0)
            + int(self.last_turn_usage.get("cache_creation_input_tokens", 0) or 0)
        )
        return {
            "session_id": self.session_id,
            "model": self.last_model,
            "cwd": self.cwd,
            "turns": self.turns,
            "last_turn_at_ms": self.last_turn_at_ms,
            "last_turn_usage": dict(self.last_turn_usage),
            "cumulative_usage": dict(self.cumulative_usage),
            "context_tokens": last_inputs,
            "attached": attached,
            "subprocess_running": subprocess_running,
            # Highest seq the session has produced to date — mirrors the
            # ``last_seq`` field on ``blemeesd.opened`` / ``blemeesd.watching``.
            "last_seq": self.seq,
        }

    def _save_usage_sidecar(self) -> None:
        if self._usage_path is None:
            return
        payload = {
            "session_id": self.session_id,
            "model": self.last_model,
            "turns": self.turns,
            "last_turn_at_ms": self.last_turn_at_ms,
            "last_turn_usage": self.last_turn_usage,
            "cumulative_usage": self.cumulative_usage,
        }
        try:
            self._usage_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._usage_path.with_suffix(".usage.json.tmp")
            tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
            tmp.replace(self._usage_path)
        except OSError:
            pass  # best-effort; ring + in-memory accumulator remain authoritative.

    def _load_usage_sidecar(self) -> None:
        if self._usage_path is None or not self._usage_path.is_file():
            return
        try:
            data = json.loads(self._usage_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        if isinstance(data.get("model"), str):
            self.last_model = data["model"]
        if isinstance(data.get("turns"), int):
            self.turns = data["turns"]
        if isinstance(data.get("last_turn_at_ms"), int):
            self.last_turn_at_ms = data["last_turn_at_ms"]
        if isinstance(data.get("last_turn_usage"), dict):
            self.last_turn_usage = dict(data["last_turn_usage"])
        if isinstance(data.get("cumulative_usage"), dict):
            for k, v in data["cumulative_usage"].items():
                if isinstance(v, int):
                    self.cumulative_usage[k] = v


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
            session_id=open_msg.session_id,
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

    def iter_with_active_turn(self) -> list[Session]:
        """Sessions whose subprocess is running and has a turn in flight."""
        return [
            s
            for s in self._sessions.values()
            if s.subprocess is not None and s.subprocess.running and s.subprocess.turn_active
        ]

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

    def remove_all_watchers_for_connection(self, connection_id: int) -> int:
        """Unsubscribe ``connection_id`` from every session it was watching.

        Returns the number of subscriptions removed. Cheap — no I/O.
        """
        n = 0
        for sess in self._sessions.values():
            if sess.remove_watcher(connection_id):
                n += 1
        return n

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
            # Remove the usage sidecar alongside the log + transcript.
            if sess._usage_path is not None:
                try:
                    if sess._usage_path.is_file():
                        sess._usage_path.unlink()
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
