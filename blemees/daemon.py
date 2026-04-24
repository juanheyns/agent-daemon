"""Daemon entry point: Unix server, connection dispatcher (spec §3–§9).

The daemon runs a single asyncio event loop. Each accepted connection spawns
a :class:`Connection` that manages its own bounded event queue, writer task,
dispatcher, and the set of sessions it owns. Sessions outlive connections.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import socket
import stat
import subprocess as _stdlib_subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import PROTOCOL_VERSION, __version__
from .config import Config
from .errors import (
    DAEMON_SHUTDOWN,
    INTERNAL,
    INVALID_MESSAGE,
    OVERSIZE_MESSAGE,
    PROTOCOL_MISMATCH,
    SESSION_BUSY,
    SESSION_UNKNOWN,
    SLOW_CONSUMER,
    SPAWN_FAILED,
    UNKNOWN_MESSAGE,
    UNSAFE_FLAG,
    BlemeesError,
    OversizeMessageError,
    ProtocolError,
    SessionBusyError,
    SessionExistsError,
    SpawnFailedError,
    UnsafeFlagError,
)
from .logging import StructuredLogger
from .protocol import (
    _MISSING,
    OpenMessage,
    PingMessage,
    StatusMessage,
    build_claude_argv,
    build_user_stdin_line,
    encode,
    error_frame,
    hello_ack,
    parse_close,
    parse_hello,
    parse_interrupt,
    parse_line,
    parse_list_sessions,
    parse_open,
    parse_ping,
    parse_session_info,
    parse_status,
    parse_unwatch,
    parse_user,
    parse_watch,
)
from .session import SessionTable, make_reaper
from .subprocess import ClaudeSubprocess, list_session_files

# Reserved `blemeesd.*` types that the daemon explicitly refuses with
# ``unknown_message`` (Appendix B). All four originally-reserved verbs
# (list_sessions, ping, status, watch) are now implemented, so this set
# is currently empty — kept as an explicit place to re-reserve names as
# future protocol additions are negotiated.
_RESERVED_TYPES: frozenset[str] = frozenset()

# How long the writer may be stuck before we declare a slow consumer.
_SLOW_CONSUMER_TIMEOUT_S = 30.0
_CONNECTION_QUEUE_SIZE = 1024
_SHUTDOWN_BUDGET_S = 5.0


def detect_claude_version(claude_bin: str) -> str | None:
    """Run ``claude --version`` once at startup. Best-effort; None on failure."""
    path = shutil.which(claude_bin) or claude_bin
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
    return (out.stdout or out.stderr or "").strip() or None


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


class Connection:
    """One client's socket connection and its owned sessions."""

    _id_seq = 0

    @classmethod
    def _next_id(cls) -> int:
        cls._id_seq += 1
        return cls._id_seq

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        config: Config,
        sessions: SessionTable,
        logger: StructuredLogger,
        claude_version: str | None,
        shutdown_event: asyncio.Event,
        lookup_connection: Callable[[int], Connection | None] | None = None,
        status_snapshot: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.id = self._next_id()
        self._reader = reader
        self._writer = writer
        self._config = config
        self._sessions = sessions
        self._claude_version = claude_version
        self._shutdown = shutdown_event
        self._lookup_connection = lookup_connection
        self._status_snapshot = status_snapshot

        self._queue: asyncio.Queue = asyncio.Queue(maxsize=_CONNECTION_QUEUE_SIZE)
        self._alive = True
        self._fatal = False
        self._writer_last_progress = time.monotonic()
        self._owned_sessions: set[str] = set()
        self._watched_sessions: set[str] = set()
        self._peer_pid: int | None = None
        self._peer_uid: int | None = None

        self._log = logger.bind(connection_id=self.id)

    # ------------------------------------------------------------------
    # Main entry
    # ------------------------------------------------------------------

    async def serve(self) -> None:
        self._capture_peer()
        self._log.info(
            "connection.open",
            peer_pid=self._peer_pid,
            peer_uid=self._peer_uid,
        )
        writer_task = asyncio.create_task(self._writer_loop(), name=f"conn-w-{self.id}")
        watchdog_task = asyncio.create_task(self._watchdog(), name=f"conn-wd-{self.id}")
        try:
            if not await self._handshake():
                return
            await self._read_loop()
        finally:
            self._alive = False
            # Put a sentinel so the writer task wakes up and exits.
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(None)
            await asyncio.gather(writer_task, return_exceptions=True)
            watchdog_task.cancel()
            with contextlib.suppress(BaseException):
                await watchdog_task
            # Detach sessions owned by this connection (spec §5.9) and
            # unsubscribe any watch subscriptions.
            await self._sessions.detach_all_for_connection(self.id)
            self._sessions.remove_all_watchers_for_connection(self.id)
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except (ConnectionError, OSError):
                pass
            self._log.info("connection.close")

    # ------------------------------------------------------------------
    # Handshake
    # ------------------------------------------------------------------

    async def _handshake(self) -> bool:
        try:
            raw = await self._reader.readuntil(b"\n")
        except asyncio.IncompleteReadError:
            return False
        except asyncio.LimitOverrunError:
            await self._send_error_sync(OVERSIZE_MESSAGE, "handshake frame too large")
            return False
        try:
            obj = parse_line(raw, max_bytes=self._config.max_line_bytes)
            if obj.get("type") != "blemeesd.hello":
                raise ProtocolError("first frame must be blemeesd.hello")
            hello = parse_hello(obj)
        except OversizeMessageError as exc:
            await self._send_error_sync(OVERSIZE_MESSAGE, exc.message)
            return False
        except ProtocolError as exc:
            await self._send_error_sync(INVALID_MESSAGE, exc.message)
            return False
        if hello.protocol != PROTOCOL_VERSION:
            await self._send_error_sync(
                PROTOCOL_MISMATCH,
                f"unsupported protocol {hello.protocol!r}, need {PROTOCOL_VERSION}",
            )
            return False
        await self._send_frame_sync(
            hello_ack(
                daemon_version=__version__,
                pid=os.getpid(),
                claude_version=self._claude_version,
            )
        )
        self._log.info("connection.hello", client=hello.client)
        return True

    # ------------------------------------------------------------------
    # Read loop
    # ------------------------------------------------------------------

    async def _read_loop(self) -> None:
        while self._alive and not self._shutdown.is_set():
            try:
                raw = await self._reader.readuntil(b"\n")
            except asyncio.IncompleteReadError as exc:
                if exc.partial:
                    self._log.debug("connection.partial_frame", length=len(exc.partial))
                return
            except asyncio.LimitOverrunError:
                await self._emit_fatal(OVERSIZE_MESSAGE, "frame exceeds stream buffer")
                return
            except (ConnectionError, OSError):
                return

            try:
                obj = parse_line(raw, max_bytes=self._config.max_line_bytes)
            except OversizeMessageError as exc:
                await self._emit_fatal(OVERSIZE_MESSAGE, exc.message)
                return
            except ProtocolError as exc:
                await self._emit_error(INVALID_MESSAGE, exc.message)
                continue

            await self._dispatch(obj)

    async def _dispatch(self, obj: dict[str, Any]) -> None:
        msg_type = obj.get("type")
        if msg_type in _RESERVED_TYPES:
            await self._emit_error(
                UNKNOWN_MESSAGE,
                f"{msg_type} is reserved for a future protocol version",
                id=obj.get("id"),
            )
            return
        try:
            if msg_type == "blemeesd.open":
                await self._handle_open(parse_open(obj))
            elif msg_type == "claude.user":
                await self._handle_user(parse_user(obj))
            elif msg_type == "blemeesd.interrupt":
                await self._handle_interrupt(parse_interrupt(obj))
            elif msg_type == "blemeesd.close":
                await self._handle_close(parse_close(obj))
            elif msg_type == "blemeesd.list_sessions":
                await self._handle_list_sessions(parse_list_sessions(obj))
            elif msg_type == "blemeesd.ping":
                await self._handle_ping(parse_ping(obj))
            elif msg_type == "blemeesd.status":
                await self._handle_status(parse_status(obj))
            elif msg_type == "blemeesd.watch":
                await self._handle_watch(parse_watch(obj))
            elif msg_type == "blemeesd.unwatch":
                await self._handle_unwatch(parse_unwatch(obj))
            elif msg_type == "blemeesd.session_info":
                await self._handle_session_info(parse_session_info(obj))
            elif msg_type == "blemeesd.hello":
                await self._emit_error(INVALID_MESSAGE, "duplicate hello", id=obj.get("id"))
            else:
                await self._emit_error(
                    UNKNOWN_MESSAGE,
                    f"unknown message type: {msg_type!r}",
                    id=obj.get("id"),
                )
        except UnsafeFlagError as exc:
            await self._emit_error(UNSAFE_FLAG, exc.message, id=obj.get("id"))
        except ProtocolError as exc:
            await self._emit_error(INVALID_MESSAGE, exc.message, id=obj.get("id"))
        except BlemeesError as exc:
            await self._emit_error(
                exc.code, exc.message, id=obj.get("id"), session_id=obj.get("session_id")
            )
        except Exception as exc:  # pragma: no cover - defensive
            self._log.exception("dispatch.internal_error", type=msg_type)
            await self._emit_error(INTERNAL, f"internal error: {exc}")

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    async def _handle_open(self, msg: OpenMessage) -> None:
        existing = self._sessions.try_get(msg.session_id)
        if existing is not None and not msg.resume:
            raise SessionExistsError(msg.session_id)

        if msg.resume:
            if existing is not None:
                # Takeover: if another connection currently owns this session,
                # tell it before we swap the writer out from under it.
                prev_id = existing.connection_id
                if (
                    prev_id is not None
                    and prev_id != self.id
                    and self._lookup_connection is not None
                ):
                    prev = self._lookup_connection(prev_id)
                    if prev is not None:
                        await prev.notify_session_taken(msg.session_id, by_peer_pid=self._peer_pid)
                existing.open_msg = msg  # refresh flags on reattach
                sess = existing
            else:
                sess = self._sessions.new_session(msg)
                await self._sessions.register(sess)
        else:
            sess = self._sessions.new_session(msg)
            await self._sessions.register(sess)

        # (Re)spawn the subprocess first so we have a pid for the ack. Any
        # events the child emits before we attach buffer into the session's
        # ring and get delivered below.
        if sess.subprocess is None or not sess.subprocess.running:
            argv = build_claude_argv(self._config.claude_bin, msg, for_resume=msg.resume)
            proc = ClaudeSubprocess(
                session_id=msg.session_id,
                argv=argv,
                cwd=msg.fields.get("cwd"),
                on_event=sess.on_event,
                logger=self._log,
                stderr_rate_lines=self._config.stderr_rate_lines,
                stderr_rate_window_s=self._config.stderr_rate_window_s,
            )
            try:
                await proc.spawn()
            except SpawnFailedError as exc:
                await self._sessions.remove(msg.session_id, delete_file=False)
                await self._emit_error(
                    SPAWN_FAILED, exc.message, id=msg.id, session_id=msg.session_id
                )
                return
            sess.subprocess = proc

        self._owned_sessions.add(msg.session_id)

        # Send ack before the event stream so clients can match the reply
        # before they start consuming (possibly replayed) frames.
        await self._emit_frame(
            {
                "type": "blemeesd.opened",
                "id": msg.id,
                "session_id": msg.session_id,
                "subprocess_pid": sess.subprocess.pid,
                "last_seq": sess.seq,
            }
        )

        # If the client asked for replay we honour it now; otherwise the
        # attach just wires live delivery and any frames queued since spawn
        # flow through immediately via the ring → writer path.
        replay = await sess.attach(
            self.id,
            self._enqueue_to_writer,
            last_seen_seq=(msg.last_seen_seq if msg.last_seen_seq is not None else 0),
        )
        self._log.info(
            "session.open",
            session_id=msg.session_id,
            resume=msg.resume,
            replayed=replay.get("replayed", 0),
            model=msg.fields.get("model"),
        )

    async def _handle_user(self, msg) -> None:
        sess = self._sessions.get(msg.session_id)
        if sess.subprocess is None or not sess.subprocess.running:
            # Respawn transparently (spec §9.1): "Next claude.user respawns via --resume"
            new_argv = build_claude_argv(self._config.claude_bin, sess.open_msg, for_resume=True)
            proc = ClaudeSubprocess(
                session_id=sess.session_id,
                argv=new_argv,
                cwd=sess.cwd,
                on_event=sess.on_event,
                logger=self._log,
                stderr_rate_lines=self._config.stderr_rate_lines,
                stderr_rate_window_s=self._config.stderr_rate_window_s,
            )
            try:
                await proc.spawn()
            except SpawnFailedError as exc:
                await self._emit_error(SPAWN_FAILED, exc.message, session_id=msg.session_id)
                return
            sess.subprocess = proc

        line = build_user_stdin_line(session_id=msg.session_id, message=msg.message)
        try:
            await sess.subprocess.send_user_line(line)
        except SessionBusyError as exc:
            await self._emit_error(SESSION_BUSY, exc.message, session_id=msg.session_id)
        except SpawnFailedError as exc:
            await self._emit_error(SPAWN_FAILED, exc.message, session_id=msg.session_id)

    async def _handle_interrupt(self, msg) -> None:
        sess = self._sessions.try_get(msg.session_id)
        if sess is None or sess.subprocess is None:
            await self._emit_frame(
                {
                    "type": "blemeesd.interrupted",
                    "session_id": msg.session_id,
                    "was_idle": True,
                }
            )
            return
        self._log.info("session.interrupt", session_id=msg.session_id)
        did_kill = await sess.subprocess.interrupt()
        await self._emit_frame(
            {
                "type": "blemeesd.interrupted",
                "session_id": msg.session_id,
                "was_idle": not did_kill,
            }
        )

    async def _handle_list_sessions(self, msg) -> None:
        on_disk = list_session_files(msg.cwd)
        merged: dict[str, dict] = {row["session_id"]: {**row, "attached": False} for row in on_disk}
        # Overlay in-memory sessions for the same cwd. Transcripts can lag
        # the first turn, so a session with no file yet still shows up.
        for sess in self._sessions.iter_by_cwd(msg.cwd):
            rec = merged.get(sess.session_id) or {"session_id": sess.session_id}
            rec["attached"] = sess.connection_id is not None
            merged[sess.session_id] = rec
        sessions = sorted(merged.values(), key=lambda r: r.get("mtime_ms") or 0, reverse=True)
        self._log.info("session.list", cwd=msg.cwd, count=len(sessions))
        await self._emit_frame(
            {
                "type": "blemeesd.sessions",
                "id": msg.id,
                "cwd": msg.cwd,
                "sessions": sessions,
            }
        )

    async def _handle_close(self, msg) -> None:
        self._log.info("session.close", session_id=msg.session_id, delete=msg.delete)
        self._owned_sessions.discard(msg.session_id)
        await self._sessions.remove(msg.session_id, delete_file=msg.delete)
        await self._emit_frame(
            {"type": "blemeesd.closed", "id": msg.id, "session_id": msg.session_id}
        )

    async def _handle_ping(self, msg: PingMessage) -> None:
        """Liveness check: reply with ``blemeesd.pong`` carrying the client's id.

        A ``data`` field on the ping is echoed back so clients can round-trip a
        correlation token without relying solely on ``id``.
        """
        frame: dict[str, Any] = {"type": "blemeesd.pong", "id": msg.id}
        if msg.data is not _MISSING:
            frame["data"] = msg.data
        await self._emit_frame(frame)

    async def _handle_status(self, msg: StatusMessage) -> None:
        """Daemon-wide introspection snapshot. No side effects.

        The snapshot payload is assembled by the :class:`Daemon` via the
        ``status_snapshot`` callable passed into this connection at accept
        time — it has visibility the per-connection handler does not (e.g.
        total connection count, daemon uptime).
        """
        snap: dict[str, Any] = self._status_snapshot() if self._status_snapshot is not None else {}
        frame = {"type": "blemeesd.status_reply", "id": msg.id, **snap}
        await self._emit_frame(frame)

    async def _handle_watch(self, msg) -> None:
        """Subscribe to a session's event stream without driving it."""
        sess = self._sessions.try_get(msg.session_id)
        if sess is None:
            await self._emit_error(
                SESSION_UNKNOWN,
                f"no such session: {msg.session_id}",
                id=msg.id,
                session_id=msg.session_id,
            )
            return
        await self._emit_frame(
            {
                "type": "blemeesd.watching",
                "id": msg.id,
                "session_id": msg.session_id,
                "last_seq": sess.seq,
            }
        )
        summary = await sess.add_watcher(
            self.id, self._enqueue_to_writer, last_seen_seq=msg.last_seen_seq
        )
        self._watched_sessions.add(msg.session_id)
        self._log.info(
            "session.watched",
            session_id=msg.session_id,
            replayed=summary.get("replayed", 0),
        )

    async def _handle_unwatch(self, msg) -> None:
        """Unsubscribe a prior ``blemeesd.watch``. No-op if not watching."""
        sess = self._sessions.try_get(msg.session_id)
        removed = False
        if sess is not None:
            removed = sess.remove_watcher(self.id)
        self._watched_sessions.discard(msg.session_id)
        await self._emit_frame(
            {
                "type": "blemeesd.unwatched",
                "id": msg.id,
                "session_id": msg.session_id,
                "was_watching": removed,
            }
        )

    async def _handle_session_info(self, msg) -> None:
        """Reply with the session's cumulative usage + per-turn snapshot.

        No side effects. Persists across daemon restarts when the durable
        event log is enabled (sidecar at ``<log_dir>/<session>.usage.json``).
        """
        sess = self._sessions.try_get(msg.session_id)
        if sess is None:
            await self._emit_error(
                SESSION_UNKNOWN,
                f"no such session: {msg.session_id}",
                id=msg.id,
                session_id=msg.session_id,
            )
            return
        subproc_running = sess.subprocess is not None and sess.subprocess.running
        snap = sess.usage_snapshot(
            attached=sess.connection_id is not None,
            subprocess_running=subproc_running,
        )
        frame: dict[str, Any] = {"type": "blemeesd.session_info_reply", "id": msg.id}
        frame.update(snap)
        await self._emit_frame(frame)

    # ------------------------------------------------------------------
    # Writer side
    # ------------------------------------------------------------------

    async def _writer_loop(self) -> None:
        while True:
            frame = await self._queue.get()
            if frame is None:
                return
            try:
                self._writer.write(encode(frame))
                await self._writer.drain()
                self._writer_last_progress = time.monotonic()
            except (ConnectionError, OSError):
                return

    async def _watchdog(self) -> None:
        while self._alive:
            await asyncio.sleep(5.0)
            if self._queue.full():
                stuck_for = time.monotonic() - self._writer_last_progress
                if stuck_for > _SLOW_CONSUMER_TIMEOUT_S:
                    self._log.warning("connection.slow_consumer", stuck_for=stuck_for)
                    await self._emit_fatal(
                        SLOW_CONSUMER,
                        f"writer stalled {stuck_for:.1f}s with full queue",
                    )
                    return

    # ------------------------------------------------------------------
    # Output helpers
    # ------------------------------------------------------------------

    async def _emit_frame(self, frame: dict[str, Any]) -> None:
        # Strip None-valued optional keys so we don't emit them with null.
        frame = {k: v for k, v in frame.items() if v is not None}
        if not self._alive:
            return
        await self._queue.put(frame)

    async def _enqueue_to_writer(self, frame: dict[str, Any]) -> None:
        """Writer callback handed to :meth:`Session.attach`.

        Sessions push tagged (``seq``-carrying) frames here. We treat them
        identically to daemon-originated frames for backpressure and
        slow-consumer purposes.
        """
        await self._emit_frame(frame)

    async def _emit_error(
        self,
        code: str,
        message: str,
        *,
        id: str | None = None,
        session_id: str | None = None,
    ) -> None:
        self._log.warning("error.emitted", code=code, session_id=session_id, id=id)
        await self._emit_frame(error_frame(code, message, id=id, session_id=session_id))

    async def _emit_fatal(self, code: str, message: str) -> None:
        self._fatal = True
        self._alive = False
        await self._send_frame_sync(error_frame(code, message))

    async def _send_frame_sync(self, frame: dict[str, Any]) -> None:
        """Write a frame directly to the socket, bypassing the queue."""
        frame = {k: v for k, v in frame.items() if v is not None}
        try:
            self._writer.write(encode(frame))
            await self._writer.drain()
        except (ConnectionError, OSError):
            pass

    async def _send_error_sync(self, code: str, message: str) -> None:
        await self._send_frame_sync(error_frame(code, message))

    # ------------------------------------------------------------------

    def _capture_peer(self) -> None:
        sock: socket.socket | None = self._writer.get_extra_info("socket")
        if sock is None:
            return
        if sys.platform == "linux":
            try:
                data = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 4 * 3)
                import struct

                pid, uid, _gid = struct.unpack("3i", data)
                self._peer_pid, self._peer_uid = pid, uid
            except OSError:
                pass
        elif sys.platform == "darwin":
            try:
                LOCAL_PEERCRED = 0x001
                data = sock.getsockopt(0, LOCAL_PEERCRED, 64)
                import struct

                # xucred layout starts with uint32 version then uint32 uid
                _ver, uid = struct.unpack_from("II", data, 0)
                self._peer_uid = uid
            except OSError:
                pass

    async def broadcast_shutdown(self) -> None:
        """Emit a daemon_shutdown error on this connection."""
        await self._send_error_sync(DAEMON_SHUTDOWN, "daemon shutting down")
        self._alive = False
        # Close the transport so that any in-progress readuntil() in
        # _read_loop() receives an EOF / ConnectionError and returns
        # immediately, allowing serve() to finish and server.wait_closed()
        # to resolve.
        with contextlib.suppress(Exception):
            self._writer.close()

    async def notify_session_taken(self, session_id: str, *, by_peer_pid: int | None) -> None:
        """Inform this connection that another connection has taken over a session.

        Emitted before the new owner is attached. The session is dropped from
        this connection's owned set so the subsequent detach-on-close doesn't
        fight the new owner over it. Events stop flowing here immediately.
        """
        self._owned_sessions.discard(session_id)
        frame: dict[str, Any] = {"type": "blemeesd.session_taken", "session_id": session_id}
        if by_peer_pid is not None:
            frame["by_peer_pid"] = by_peer_pid
        await self._emit_frame(frame)
        self._log.info("session.taken_notified", session_id=session_id, by_peer_pid=by_peer_pid)


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------


class Daemon:
    def __init__(self, config: Config, logger: StructuredLogger) -> None:
        self._config = config
        self._log = logger
        self._sessions = SessionTable(
            idle_timeout_s=config.idle_timeout_s,
            max_concurrent=config.max_concurrent_sessions,
            ring_buffer_size=config.ring_buffer_size,
            event_log_dir=config.event_log_dir,
        )
        self._server: asyncio.AbstractServer | None = None
        self._connections: set[Connection] = set()
        self._shutdown_event = asyncio.Event()
        self._reaper_task: asyncio.Task | None = None
        self._claude_version: str | None = None
        self._start_time: float = time.monotonic()

    async def start(self) -> None:
        self._claude_version = detect_claude_version(self._config.claude_bin)
        _prepare_socket_path(self._config.socket_path, self._log)

        self._server = await asyncio.start_unix_server(
            self._on_client,
            path=self._config.socket_path,
        )
        os.chmod(self._config.socket_path, 0o600)
        self._reaper_task = make_reaper(self._sessions, self._log)
        self._log.info(
            "daemon.start",
            socket=self._config.socket_path,
            pid=os.getpid(),
            claude_version=self._claude_version,
            version=__version__,
        )

    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        conn = Connection(
            reader,
            writer,
            config=self._config,
            sessions=self._sessions,
            logger=self._log,
            claude_version=self._claude_version,
            shutdown_event=self._shutdown_event,
            lookup_connection=self._lookup_connection,
            status_snapshot=self._status_snapshot,
        )
        self._connections.add(conn)
        try:
            await conn.serve()
        finally:
            self._connections.discard(conn)

    def _lookup_connection(self, connection_id: int) -> Connection | None:
        for c in self._connections:
            if c.id == connection_id:
                return c
        return None

    def _status_snapshot(self) -> dict[str, Any]:
        """Assemble the payload for a ``blemeesd.status_reply`` frame."""
        now = time.monotonic()
        sessions = list(self._sessions._sessions.values())
        total = len(sessions)
        attached = sum(1 for s in sessions if s.connection_id is not None)
        active = len(self._sessions.iter_with_active_turn())
        return {
            "daemon": f"blemeesd/{__version__}",
            "protocol": PROTOCOL_VERSION,
            "pid": os.getpid(),
            "claude_version": self._claude_version,
            "uptime_s": round(now - self._start_time, 3),
            "socket_path": self._config.socket_path,
            "connections": len(self._connections),
            "sessions": {
                "total": total,
                "attached": attached,
                "detached": total - attached,
                "active_turns": active,
            },
            "config": {
                "ring_buffer_size": self._config.ring_buffer_size,
                "event_log_enabled": bool(self._config.event_log_dir),
                "idle_timeout_s": self._config.idle_timeout_s,
                "shutdown_grace_s": self._config.shutdown_grace_s,
                "max_concurrent_sessions": self._config.max_concurrent_sessions,
                "max_line_bytes": self._config.max_line_bytes,
            },
        }

    async def serve_forever(self) -> None:
        assert self._server is not None
        try:
            await self._shutdown_event.wait()
        finally:
            await self._shutdown()

    def request_shutdown(self) -> None:
        self._log.info("daemon.shutdown_requested")
        self._shutdown_event.set()

    async def _shutdown(self) -> None:
        if self._server is not None:
            self._server.close()

        # Notify live connections and close their transports so that any
        # pending readuntil() calls unblock immediately.  This must happen
        # before server.wait_closed(), because wait_closed() in Python 3.12+
        # waits for *all* active _on_client callbacks to return, and those
        # callbacks are blocked in _read_loop until the transport is closed.
        for conn in list(self._connections):
            with contextlib.suppress(Exception):
                await conn.broadcast_shutdown()

        # Wait for all _on_client callbacks to finish now that connections
        # have been told to close.  Use a short timeout so a stuck connection
        # can't prevent the rest of the shutdown sequence from running.
        if self._server is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), timeout=_SHUTDOWN_BUDGET_S)

        # Graceful phase: let sessions with an in-flight turn run to the next
        # claude.result so their transcript closes cleanly. Same soft-detach
        # policy as client disconnect (§5.9). Capped by shutdown_grace_s.
        grace = self._config.shutdown_grace_s
        active = self._sessions.iter_with_active_turn()
        for sess in active:
            sess.mark_finishing()
        if active and grace > 0:
            self._log.info(
                "daemon.shutdown_waiting_for_turns",
                count=len(active),
                grace_s=grace,
            )
            try:
                await asyncio.wait_for(
                    asyncio.gather(
                        *(s.subprocess.wait_for_exit(grace) for s in active),
                        return_exceptions=True,
                    ),
                    timeout=grace,
                )
            except TimeoutError:
                self._log.warning("daemon.shutdown_grace_expired", still_running=len(active))

        # Force phase: kill anything still alive.
        try:
            await asyncio.wait_for(self._sessions.shutdown(), timeout=_SHUTDOWN_BUDGET_S)
        except TimeoutError:
            self._log.warning("daemon.shutdown_timeout")

        if self._reaper_task is not None:
            self._reaper_task.cancel()
            with contextlib.suppress(BaseException):
                await self._reaper_task

        # Unlink the socket file.
        try:
            os.unlink(self._config.socket_path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            self._log.warning("daemon.socket_unlink_failed", error=str(exc))

        self._log.info("daemon.stop")


def _prepare_socket_path(path: str, logger: StructuredLogger) -> None:
    """Enforce the 0600 ownership invariant and clear stale socket files.

    * If a non-socket file exists at ``path``, refuse to start.
    * If a socket exists and connects, another daemon is live → exit 1.
    * If a socket exists but connect fails, unlink as stale.
    * If the path exists and is not owned by our UID, refuse to start.
    """
    p = Path(path)
    parent = p.parent
    parent.mkdir(parents=True, exist_ok=True)

    if not p.exists() and not p.is_symlink():
        return

    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return

    if not stat.S_ISSOCK(st.st_mode):
        logger.error("daemon.socket_path_not_socket", path=path)
        raise SystemExit(1)

    if st.st_uid != os.getuid():
        logger.error("daemon.socket_not_owned", path=path, uid=st.st_uid)
        raise SystemExit(1)

    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        s.connect(path)
    except (ConnectionRefusedError, FileNotFoundError, OSError):
        # Stale socket — remove and continue.
        try:
            os.unlink(path)
        except OSError:
            pass
        logger.info("daemon.removed_stale_socket", path=path)
        return
    else:
        logger.error("daemon.another_instance_running", path=path)
        raise SystemExit(1)
    finally:
        s.close()


async def run_daemon(config: Config, logger: StructuredLogger) -> int:
    daemon = Daemon(config, logger)
    await daemon.start()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, daemon.request_shutdown)

    try:
        await daemon.serve_forever()
    except asyncio.CancelledError:  # pragma: no cover - defensive
        await daemon._shutdown()
    return 0
