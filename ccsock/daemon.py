"""Daemon entry point: Unix server, connection dispatcher (spec §3–§9).

The daemon runs a single asyncio event loop. Each accepted connection spawns
a :class:`Connection` that manages its own bounded event queue, writer task,
dispatcher, and the set of sessions it owns. Sessions outlive connections.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import socket
import stat
import subprocess as _stdlib_subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import PROTOCOL_VERSION, __version__
from .config import Config
from .errors import (
    CcsockError,
    DAEMON_SHUTDOWN,
    INTERNAL,
    INVALID_MESSAGE,
    OVERSIZE_MESSAGE,
    PROTOCOL_MISMATCH,
    SLOW_CONSUMER,
    SPAWN_FAILED,
    SESSION_BUSY,
    SESSION_UNKNOWN,
    UNKNOWN_MESSAGE,
    UNSAFE_FLAG,
    OversizeMessageError,
    ProtocolError,
    SpawnFailedError,
    SessionBusyError,
    SessionExistsError,
    SessionUnknownError,
    UnsafeFlagError,
)
from .logging import StructuredLogger
from .protocol import (
    OpenMessage,
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
    parse_user,
)
from .session import Session, SessionTable, make_reaper
from .subprocess import ClaudeSubprocess, list_session_files


# Reserved `ccsockd.*` types that v0.1 explicitly refuses with
# ``unknown_message`` (Appendix B). ``list_sessions`` was reserved in the
# original spec but unreserved here in v0.1.1 — clients need parity with
# the interactive ``/resume`` discovery flow.
_RESERVED_TYPES = frozenset(
    {
        "ccsockd.ping",
        "ccsockd.pong",
        "ccsockd.status",
        "ccsockd.watch",
    }
)

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
    ) -> None:
        self.id = self._next_id()
        self._reader = reader
        self._writer = writer
        self._config = config
        self._sessions = sessions
        self._claude_version = claude_version
        self._shutdown = shutdown_event

        self._queue: asyncio.Queue = asyncio.Queue(maxsize=_CONNECTION_QUEUE_SIZE)
        self._alive = True
        self._fatal = False
        self._writer_last_progress = time.monotonic()
        self._owned_sessions: set[str] = set()
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
            # Detach sessions owned by this connection (spec §5.9).
            await self._sessions.detach_all_for_connection(self.id)
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
            if obj.get("type") != "ccsockd.hello":
                raise ProtocolError("first frame must be ccsockd.hello")
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
            if msg_type == "ccsockd.open":
                await self._handle_open(parse_open(obj))
            elif msg_type == "ccsockd.user":
                await self._handle_user(parse_user(obj))
            elif msg_type == "ccsockd.interrupt":
                await self._handle_interrupt(parse_interrupt(obj))
            elif msg_type == "ccsockd.close":
                await self._handle_close(parse_close(obj))
            elif msg_type == "ccsockd.list_sessions":
                await self._handle_list_sessions(parse_list_sessions(obj))
            elif msg_type == "ccsockd.hello":
                await self._emit_error(
                    INVALID_MESSAGE, "duplicate hello", id=obj.get("id")
                )
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
        except CcsockError as exc:
            await self._emit_error(exc.code, exc.message, id=obj.get("id"), session=obj.get("session"))
        except Exception as exc:  # pragma: no cover - defensive
            self._log.exception("dispatch.internal_error", type=msg_type)
            await self._emit_error(INTERNAL, f"internal error: {exc}")

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    async def _handle_open(self, msg: OpenMessage) -> None:
        existing = self._sessions.try_get(msg.session)
        if existing is not None and not msg.resume:
            raise SessionExistsError(msg.session)

        if msg.resume:
            sess: Session
            if existing is not None:
                # Reattach detached session, reuse recorded open_msg for consistency.
                if existing.subprocess is not None and existing.subprocess.running:
                    existing.connection_id = self.id
                    existing.detached_at = None
                    sess = existing
                else:
                    existing.connection_id = self.id
                    existing.detached_at = None
                    existing.open_msg = msg  # update flags on reattach
                    sess = existing
            else:
                sess = Session(
                    session_id=msg.session,
                    open_msg=msg,
                    cwd=msg.fields.get("cwd"),
                    connection_id=self.id,
                )
                await self._sessions.register(sess)
        else:
            sess = Session(
                session_id=msg.session,
                open_msg=msg,
                cwd=msg.fields.get("cwd"),
                connection_id=self.id,
            )
            await self._sessions.register(sess)

        # (Re)spawn the subprocess if not already running.
        if sess.subprocess is None or not sess.subprocess.running:
            argv = build_claude_argv(self._config.claude_bin, msg, for_resume=msg.resume)
            proc = ClaudeSubprocess(
                session_id=msg.session,
                argv=argv,
                cwd=msg.fields.get("cwd"),
                event_queue=self._queue,  # type: ignore[arg-type]
                logger=self._log,
                stderr_rate_lines=self._config.stderr_rate_lines,
                stderr_rate_window_s=self._config.stderr_rate_window_s,
            )
            try:
                await proc.spawn()
            except SpawnFailedError as exc:
                await self._sessions.remove(msg.session, delete_file=False)
                await self._emit_error(
                    SPAWN_FAILED, exc.message, id=msg.id, session=msg.session
                )
                return
            sess.subprocess = proc

        self._owned_sessions.add(msg.session)
        self._log.info(
            "session.open",
            session_id=msg.session,
            resume=msg.resume,
            model=msg.fields.get("model"),
        )
        await self._emit_frame(
            {
                "type": "ccsockd.opened",
                "id": msg.id,
                "session": msg.session,
                "subprocess_pid": sess.subprocess.pid,
            }
        )

    async def _handle_user(self, msg) -> None:
        sess = self._sessions.get(msg.session)
        if sess.subprocess is None or not sess.subprocess.running:
            # Respawn transparently (spec §9.1): "Next ccsockd.user respawns via --resume"
            new_argv = build_claude_argv(
                self._config.claude_bin, sess.open_msg, for_resume=True
            )
            proc = ClaudeSubprocess(
                session_id=sess.session_id,
                argv=new_argv,
                cwd=sess.cwd,
                event_queue=self._queue,  # type: ignore[arg-type]
                logger=self._log,
                stderr_rate_lines=self._config.stderr_rate_lines,
                stderr_rate_window_s=self._config.stderr_rate_window_s,
            )
            try:
                await proc.spawn()
            except SpawnFailedError as exc:
                await self._emit_error(
                    SPAWN_FAILED, exc.message, session=msg.session
                )
                return
            sess.subprocess = proc

        line = build_user_stdin_line(msg.session, text=msg.text, content=msg.content)
        try:
            await sess.subprocess.send_user_line(line)
        except SessionBusyError as exc:
            await self._emit_error(SESSION_BUSY, exc.message, session=msg.session)
        except SpawnFailedError as exc:
            await self._emit_error(SPAWN_FAILED, exc.message, session=msg.session)

    async def _handle_interrupt(self, msg) -> None:
        sess = self._sessions.try_get(msg.session)
        if sess is None or sess.subprocess is None:
            await self._emit_frame(
                {
                    "type": "ccsockd.interrupted",
                    "session": msg.session,
                    "was_idle": True,
                }
            )
            return
        self._log.info("session.interrupt", session_id=msg.session)
        did_kill = await sess.subprocess.interrupt()
        await self._emit_frame(
            {
                "type": "ccsockd.interrupted",
                "session": msg.session,
                "was_idle": not did_kill,
            }
        )

    async def _handle_list_sessions(self, msg) -> None:
        on_disk = list_session_files(msg.cwd)
        merged: dict[str, dict] = {
            row["session"]: {**row, "attached": False} for row in on_disk
        }
        # Overlay in-memory sessions for the same cwd. Transcripts can lag
        # the first turn, so a session with no file yet still shows up.
        for sess in self._sessions.iter_by_cwd(msg.cwd):
            rec = merged.get(sess.session_id) or {"session": sess.session_id}
            rec["attached"] = sess.connection_id is not None
            merged[sess.session_id] = rec
        sessions = sorted(
            merged.values(), key=lambda r: r.get("mtime_ms") or 0, reverse=True
        )
        self._log.info(
            "session.list", cwd=msg.cwd, count=len(sessions)
        )
        await self._emit_frame(
            {
                "type": "ccsockd.sessions",
                "id": msg.id,
                "cwd": msg.cwd,
                "sessions": sessions,
            }
        )

    async def _handle_close(self, msg) -> None:
        self._log.info(
            "session.close", session_id=msg.session, delete=msg.delete
        )
        self._owned_sessions.discard(msg.session)
        await self._sessions.remove(msg.session, delete_file=msg.delete)
        await self._emit_frame(
            {"type": "ccsockd.closed", "id": msg.id, "session": msg.session}
        )

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

    async def _emit_error(
        self,
        code: str,
        message: str,
        *,
        id: str | None = None,
        session: str | None = None,
    ) -> None:
        self._log.warning("error.emitted", code=code, session_id=session, id=id)
        await self._emit_frame(error_frame(code, message, id=id, session=session))

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
                data = sock.getsockopt(
                    socket.SOL_SOCKET, socket.SO_PEERCRED, 4 * 3
                )
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
        )
        self._server: asyncio.AbstractServer | None = None
        self._connections: set[Connection] = set()
        self._shutdown_event = asyncio.Event()
        self._reaper_task: asyncio.Task | None = None
        self._claude_version: str | None = None

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

    async def _on_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        conn = Connection(
            reader,
            writer,
            config=self._config,
            sessions=self._sessions,
            logger=self._log,
            claude_version=self._claude_version,
            shutdown_event=self._shutdown_event,
        )
        self._connections.add(conn)
        try:
            await conn.serve()
        finally:
            self._connections.discard(conn)

    async def serve_forever(self) -> None:
        assert self._server is not None
        try:
            async with self._server:
                await self._shutdown_event.wait()
        finally:
            await self._shutdown()

    def request_shutdown(self) -> None:
        self._log.info("daemon.shutdown_requested")
        self._shutdown_event.set()

    async def _shutdown(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()

        # Notify live connections.
        for conn in list(self._connections):
            with contextlib.suppress(Exception):
                await conn.broadcast_shutdown()

        # Tear down sessions; their subprocesses are SIGTERM'd with 500 ms grace.
        try:
            await asyncio.wait_for(
                self._sessions.shutdown(), timeout=_SHUTDOWN_BUDGET_S
            )
        except asyncio.TimeoutError:
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
