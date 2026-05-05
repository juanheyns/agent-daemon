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

from .backends import AgentBackend
from .errors import SessionExistsError, SessionUnknownError
from .event_log import DurableEventLog, RingBuffer, event_log_path
from .protocol import OpenMessage

WriterFn = Callable[[dict], Awaitable[None]]


@dataclass(slots=True)
class Session:
    session_id: str
    open_msg: OpenMessage
    cwd: str | None
    backend_name: str = "claude"
    connection_id: int | None = None
    backend: AgentBackend | None = None
    detached_at: float | None = None

    # Backend-side session identifier surfaced on `agent.system_init`.
    # Equal to ``session_id`` for the Claude backend; the Codex
    # ``threadId`` for Codex (only known after the first turn produces
    # ``session_configured``). Used by:
    #   * ``blemeesd.opened.native_session_id`` (omitted until known).
    #   * ``CodexBackend`` on resume so the first ``tools/call`` routes
    #     through ``codex-reply`` with the cached id.
    native_session_id: str | None = None
    # Rollout transcript path Codex writes to (carried on
    # ``agent.system_init.capabilities.rollout_path``). Cached so
    # ``blemeesd.close{delete:true}`` can unlink it without re-scanning
    # the rollout directory.
    rollout_path: str | None = None

    # Event-stream state -------------------------------------------------
    seq: int = 0
    ring: RingBuffer = field(default_factory=lambda: RingBuffer(1024))
    log: DurableEventLog | None = None
    _writer: WriterFn | None = None
    _finishing: bool = False  # subprocess keeps running, kill on next result

    # Non-driving subscribers: connection_id → writer. Watchers receive every
    # frame the primary writer gets (agent.* events, blemeesd.stderr,
    # blemeesd.error{backend_crashed,auth_failed}, and replays on subscribe)
    # but cannot drive the session (user/interrupt/close).
    _watchers: dict[int, WriterFn] = field(default_factory=dict)

    # Running usage accumulator maintained from agent.result frames. Persists
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

    # Wall-clock when the session was first registered; surfaced on
    # `blemeesd.live_sessions` so clients can sort by age. Set on
    # ``SessionTable.new_session``.
    started_at_ms: int | None = None
    # Daemon-derived title from the first observed user message, capped
    # at 80 characters. Absent until the session has driven a turn.
    title: str | None = None

    extra: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Event dispatch
    # ------------------------------------------------------------------

    async def on_event(self, frame: dict) -> None:
        """Called by the backend for every event it emits.

        Tags with ``session_id`` and ``seq``, appends to the ring + durable
        log, and pushes to the attached writer (if any). If we're in
        ``finishing`` mode and this is the turn-ending ``agent.result``,
        schedule a clean kill.
        """
        # Backends emit translator output without session_id; layering it on
        # here keeps the translator stateless and gives us one place that owns
        # the session-tagging contract.
        frame.setdefault("session_id", self.session_id)
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
        # Soft-kill after a completed turn when the client has left.
        # Backends emit `agent.result` as the turn-ending frame.
        if self._finishing and frame.get("type") == "agent.result":
            self._finishing = False
            sub = self.backend
            if sub is not None:
                asyncio.create_task(sub.close(), name=f"backend-soft-kill-{self.session_id}")

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

    async def broadcast_to_watchers(self, frame: dict) -> None:
        """Send a one-shot notification frame to every watcher.

        Unlike :meth:`on_event`, the frame is **not** seq-tagged, **not**
        appended to the ring, and **not** persisted. It is purely a
        connection-level signal (e.g. ``blemeesd.session_closed``). Dead
        writers are silently dropped, matching the fan-out policy in
        ``on_event``.
        """
        if not self._watchers:
            return
        dead: list[int] = []
        for conn_id, w in self._watchers.items():
            try:
                await w(frame)
            except Exception:
                dead.append(conn_id)
        for conn_id in dead:
            self._watchers.pop(conn_id, None)

    # ------------------------------------------------------------------
    # Title (derived from first user message)
    # ------------------------------------------------------------------

    _TITLE_MAX_CHARS = 80

    def record_user_message(self, message: dict) -> None:
        """Capture a session title from the first observed user turn.

        No-op if a title is already set, or if the message has no
        extractable text. Multimodal arrays concatenate their text
        blocks (non-text blocks are skipped).
        """
        if self.title is not None:
            return
        content = message.get("content")
        text: str | None = None
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    t = block.get("text")
                    if isinstance(t, str):
                        parts.append(t)
            if parts:
                text = " ".join(parts)
        if not text:
            return
        # Collapse newlines / runs of whitespace before the cap so the
        # title reads as a single line in a sidebar.
        compact = " ".join(text.split())
        if not compact:
            return
        if len(compact) > self._TITLE_MAX_CHARS:
            compact = compact[: self._TITLE_MAX_CHARS - 1].rstrip() + "…"
        self.title = compact

    # ------------------------------------------------------------------
    # Live summary (for blemeesd.list_live_sessions)
    # ------------------------------------------------------------------

    def live_summary(self, *, owner_pid: int | None) -> dict[str, Any]:
        """Build one row for a ``blemeesd.live_sessions`` reply.

        Optional fields (``cwd``, ``model``, ``title``, ``owner_pid``,
        ``last_active_at_ms``) are omitted entirely when not known —
        callers should not see ``null`` on the wire (matches the
        spec-wide convention for absent optional fields).
        """
        out: dict[str, Any] = {
            "session_id": self.session_id,
            "backend": self.backend_name,
            "attached": self.connection_id is not None,
            "last_seq": self.seq,
            "turn_active": (
                self.backend is not None and self.backend.running and self.backend.turn_active
            ),
        }
        if self.cwd:
            out["cwd"] = self.cwd
        if self.last_model:
            out["model"] = self.last_model
        if self.title:
            out["title"] = self.title
        if self.started_at_ms is not None:
            out["started_at_ms"] = self.started_at_ms
        last_active = self.last_turn_at_ms or self.started_at_ms
        if last_active is not None:
            out["last_active_at_ms"] = last_active
        if self.connection_id is not None and owner_pid is not None:
            out["owner_pid"] = owner_pid
        return out

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
        """Pull model / usage out of normalised agent.* events. Persist if enabled."""
        t = frame.get("type")
        if t == "agent.system_init":
            model = frame.get("model")
            if isinstance(model, str):
                self.last_model = model
            native = frame.get("native_session_id")
            if isinstance(native, str) and native:
                self.native_session_id = native
            caps = frame.get("capabilities")
            if isinstance(caps, dict):
                rollout = caps.get("rollout_path")
                if isinstance(rollout, str) and rollout:
                    self.rollout_path = rollout
            # Persist so resume across daemon restarts can recover both.
            self._save_usage_sidecar()
            return
        if t != "agent.result":
            return
        usage = frame.get("usage")
        if not isinstance(usage, dict):
            return
        # Snapshot last-turn usage verbatim so clients see whatever fields a
        # backend emits (including future ones we don't know about yet).
        self.last_turn_usage = dict(usage)
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
            "reasoning_output_tokens",
        ):
            v = usage.get(key)
            if isinstance(v, int):
                self.cumulative_usage[key] = self.cumulative_usage.get(key, 0) + v
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
            "backend": self.backend_name,
            "native_session_id": self.native_session_id or self.session_id,
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
            "native_session_id": self.native_session_id,
            "rollout_path": self.rollout_path,
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
        if isinstance(data.get("native_session_id"), str):
            self.native_session_id = data["native_session_id"]
        if isinstance(data.get("rollout_path"), str):
            self.rollout_path = data["rollout_path"]


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
            cwd=open_msg.options.get("cwd"),
            backend_name=open_msg.backend,
            ring=RingBuffer(self.ring_buffer_size),
            started_at_ms=int(time.time() * 1000),
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
        """Sessions whose backend is running and has a turn in flight."""
        return [
            s
            for s in self._sessions.values()
            if s.backend is not None and s.backend.running and s.backend.turn_active
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
        sub = sess.backend
        if sub is None or not sub.running:
            sess.backend = None
            return
        if sub.turn_active:
            sess.mark_finishing()
        else:
            await sub.close()
            sess.backend = None

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
        if sess.backend is not None:
            await sess.backend.close()
        if sess.log is not None:
            if delete_file:
                sess.log.unlink()
            else:
                sess.log.close()
        if delete_file:
            # Backend-native transcripts under `~/.claude/projects/` and
            # `~/.codex/sessions/` are *not* removed: they live under
            # directories the backends own and (for Codex) reference from
            # an internal state DB. Deleting behind their backs surfaces
            # as ERROR-level stderr noise (e.g. codex's "state db returned
            # stale rollout path …") and breaks resume-from-disk for
            # clients that didn't ask the daemon to do that. The daemon
            # only removes its own files: the event log and the usage
            # sidecar.
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
