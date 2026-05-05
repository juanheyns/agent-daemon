"""Reference Python client for blemeesd (Appendix A; stdlib only).

Usage::

    async with BlemeesClient.connect() as c:
        async with c.open_session(
            session_id="s1",
            backend="claude",
            options={"model": "sonnet", "tools": ""},
        ) as s:
            await s.send_user("hi")
            async for evt in s.events():
                if evt.get("type") == "agent.result":
                    break
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from . import PROTOCOL_VERSION


def default_socket_path() -> str:
    env = os.environ.get("BLEMEES_AGENTD_SOCKET")
    if env:
        return env
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return str(Path(xdg) / "blemees" / "agentd.sock")
    return f"/tmp/blemees-agentd-{os.getuid()}.sock"


class BlemeesClientError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class Session:
    def __init__(self, client: BlemeesClient, session_id: str) -> None:
        self._client = client
        self.session_id = session_id
        self._queue: asyncio.Queue = asyncio.Queue()
        self._closed = False
        self.last_seq: int = 0  # highest seq observed; pass into open(resume)

    async def send_user(
        self,
        text: str | None = None,
        *,
        content: list | None = None,
        message: dict | None = None,
    ) -> None:
        """Send a user turn.

        Pass ``message={"role":"user","content":...}`` for the raw wire
        shape. For convenience, ``text="..."`` or ``content=[...]`` will
        be wrapped into that shape before sending.
        """
        if message is None:
            payload: object = content if content is not None else (text or "")
            message = {"role": "user", "content": payload}
        await self._client._send(
            {"type": "agent.user", "session_id": self.session_id, "message": message}
        )

    async def interrupt(self) -> None:
        await self._client._send({"type": "agent.interrupt", "session_id": self.session_id})

    async def close(self, *, delete: bool = False) -> None:
        if self._closed:
            return
        self._closed = True
        await self._client._send(
            {"type": "agent.close", "session_id": self.session_id, "delete": delete}
        )

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            evt = await self._queue.get()
            if evt is None:
                return
            yield evt

    def _deliver(self, evt: dict[str, Any]) -> None:
        seq = evt.get("seq")
        if isinstance(seq, int) and seq > self.last_seq:
            self.last_seq = seq
        self._queue.put_nowait(evt)

    def _terminate(self) -> None:
        self._queue.put_nowait(None)


class BlemeesClient:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._write_lock = asyncio.Lock()
        self._sessions: dict[str, Session] = {}
        self._pending: dict[str, asyncio.Future] = {}
        self._next_req = 0
        self._reader_task: asyncio.Task | None = None
        self.daemon_info: dict[str, Any] = {}

    @classmethod
    async def connect(cls, socket_path: str | None = None) -> BlemeesClient:
        path = socket_path or default_socket_path()
        reader, writer = await asyncio.open_unix_connection(path)
        client = cls(reader, writer)
        await client._send(
            {
                "type": "agent.hello",
                "client": "blemees-reference/0.1",
                "protocol": PROTOCOL_VERSION,
            }
        )
        ack = await client._read_one()
        if ack.get("type") != "agent.hello_ack":
            raise BlemeesClientError(ack.get("code", "protocol"), ack.get("message", str(ack)))
        client.daemon_info = ack
        client._reader_task = asyncio.create_task(client._reader_loop())
        return client

    async def __aenter__(self) -> BlemeesClient:
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.close()

    async def list_sessions(self, cwd: str) -> list[dict[str, Any]]:
        """Return past sessions for ``cwd`` (on-disk + currently attached).

        Results are sorted newest-first and each record has at least
        ``session_id`` and ``attached``; on-disk records also carry ``mtime_ms``,
        ``size``, and an optional ``preview`` of the first user message.
        """
        self._next_req += 1
        req_id = f"req_{self._next_req}"
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        await self._send({"type": "agent.list_sessions", "id": req_id, "cwd": cwd})
        reply = await fut
        if reply.get("type") == "agent.error":
            raise BlemeesClientError(reply.get("code", ""), reply.get("message", ""))
        return list(reply.get("sessions", []))

    @contextlib.asynccontextmanager
    async def open_session(
        self,
        *,
        session_id: str,
        backend: str = "claude",
        options: dict[str, Any] | None = None,
        resume: bool = False,
        last_seen_seq: int | None = None,
    ) -> AsyncIterator[Session]:
        self._next_req += 1
        req_id = f"req_{self._next_req}"
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        sess = Session(self, session_id)
        self._sessions[session_id] = sess
        frame: dict[str, Any] = {
            "type": "agent.open",
            "id": req_id,
            "session_id": session_id,
            "backend": backend,
            "options": {backend: dict(options or {})},
        }
        if resume:
            frame["resume"] = True
        if last_seen_seq is not None:
            frame["last_seen_seq"] = last_seen_seq
        await self._send(frame)
        reply = await fut
        if reply.get("type") == "agent.error":
            self._sessions.pop(session_id, None)
            raise BlemeesClientError(reply.get("code", ""), reply.get("message", ""))
        try:
            yield sess
        finally:
            await sess.close()
            self._sessions.pop(session_id, None)

    async def close(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(BaseException):
                await self._reader_task
        for sess in list(self._sessions.values()):
            sess._terminate()
        try:
            self._writer.close()
            await self._writer.wait_closed()
        except (ConnectionError, OSError):
            pass

    async def _send(self, frame: dict[str, Any]) -> None:
        data = (json.dumps(frame, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        async with self._write_lock:
            self._writer.write(data)
            await self._writer.drain()

    async def _read_one(self) -> dict[str, Any]:
        raw = await self._reader.readuntil(b"\n")
        return json.loads(raw.rstrip(b"\r\n").decode("utf-8"))

    async def _reader_loop(self) -> None:
        try:
            while True:
                raw = await self._reader.readuntil(b"\n")
                evt = json.loads(raw.rstrip(b"\r\n").decode("utf-8"))
                req_id = evt.get("id")
                msg_type = evt.get("type")
                if (
                    req_id
                    and msg_type
                    in {
                        "agent.opened",
                        "agent.closed",
                        "agent.sessions",
                        "agent.error",
                    }
                    and req_id in self._pending
                ):
                    fut = self._pending.pop(req_id)
                    if not fut.done():
                        fut.set_result(evt)
                session_id = evt.get("session_id")
                if session_id and session_id in self._sessions:
                    self._sessions[session_id]._deliver(evt)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(BlemeesClientError("closed", "connection closed"))
            for sess in self._sessions.values():
                sess._terminate()
