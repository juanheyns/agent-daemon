"""Shared pytest fixtures for blemees tests."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from blemees import PROTOCOL_VERSION
from blemees.config import Config
from blemees.daemon import Daemon
from blemees.logging import configure

FAKE_CLAUDE = Path(__file__).parent / "fake_claude.py"


def short_socket_path(name: str = "blemeesd") -> Path:
    """Return a short-ish Unix-socket path.

    macOS's ``sun_path`` is 104 bytes (Linux is 108). pytest's ``tmp_path`` on
    macOS CI lives under ``/Users/runner/work/_temp/pytest-of-runner/...``,
    which routinely overflows that limit when a session id / subdir is
    appended. We bind sockets under the system temp dir (e.g. ``/tmp``)
    with an 8-char random suffix so the full path stays well under 104.
    """
    tag = secrets.token_hex(4)
    return Path(tempfile.gettempdir()) / f"{name}-{tag}.sock"


@contextlib.contextmanager
def socket_cleanup(path: Path):
    """Best-effort unlink of a socket path after the test, if the daemon
    crashed before its own _prepare_socket_path unlink fired."""
    try:
        yield path
    finally:
        with contextlib.suppress(FileNotFoundError, OSError):
            path.unlink()


@pytest.fixture
def fake_claude_bin() -> str:
    """Return an invoker that runs the fake claude via the current Python."""
    # The daemon uses asyncio.create_subprocess_exec; we can't trivially pass
    # multi-token argv, so we wrap by writing a tiny shim script.
    return str(FAKE_CLAUDE)


@pytest.fixture
def argv_trace_path(tmp_path):
    path = tmp_path / "argv_trace.jsonl"
    return path


@pytest.fixture
def fake_mode(monkeypatch):
    def _set(mode: str) -> None:
        monkeypatch.setenv("BLEMEES_FAKE_MODE", mode)

    return _set


@pytest_asyncio.fixture
async def daemon_and_socket(tmp_path, argv_trace_path, monkeypatch, request):
    """Start a daemon bound to a tmp socket using the fake claude stub.

    Yields ``(Daemon, socket_path)``. The daemon is shut down on teardown.

    Tests may parametrize extra config via ``indirect``-style attrs on the
    request's ``node.stash`` or by reading the ``daemon_config`` fixture.
    """
    monkeypatch.setenv("BLEMEES_FAKE_ARGV_FILE", str(argv_trace_path))
    monkeypatch.setenv("BLEMEES_FAKE_MODE", os.environ.get("BLEMEES_FAKE_MODE", "normal"))

    # macOS sun_path is 104 bytes; pytest's tmp_path on macOS CI overflows
    # that limit for nested socket files. Bind under /tmp instead.
    socket_path = short_socket_path("blemeesd-test")
    overrides = getattr(request, "param", None) or {}
    cfg = Config(
        socket_path=str(socket_path),
        claude_bin=f"{sys.executable}",  # will be overridden below
        idle_timeout_s=60,
        max_concurrent_sessions=8,
        ring_buffer_size=overrides.get("ring_buffer_size", 1024),
        event_log_dir=overrides.get("event_log_dir"),
    )
    # We can't spawn "python fake_claude.py" with create_subprocess_exec using
    # a single binary; use the fake script's shebang by making the "binary"
    # path the python interpreter and injecting a wrapper. Simpler: swap
    # claude_bin to point at the fake script directly (shebang runs it).
    cfg.claude_bin = str(FAKE_CLAUDE)

    logger = configure("error")
    daemon = Daemon(cfg, logger)
    await daemon.start()
    serve_task = asyncio.create_task(daemon.serve_forever())
    try:
        yield daemon, str(socket_path)
    finally:
        daemon.request_shutdown()
        try:
            await asyncio.wait_for(serve_task, timeout=5.0)
        except TimeoutError:
            serve_task.cancel()


class _StreamClient:
    """Minimal direct-wire client used by the mock tests.

    Keeps a background reader so callers can wait on specific event types.
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._closed = False
        self._task = asyncio.create_task(self._pump())

    async def _pump(self) -> None:
        try:
            while True:
                raw = await self.reader.readuntil(b"\n")
                await self._queue.put(json.loads(raw.rstrip(b"\r\n").decode("utf-8")))
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            await self._queue.put({"type": "__closed__"})

    async def send(self, frame: dict[str, Any]) -> None:
        data = (json.dumps(frame) + "\n").encode("utf-8")
        self.writer.write(data)
        await self.writer.drain()

    async def recv(self, timeout: float = 5.0) -> dict[str, Any]:
        return await asyncio.wait_for(self._queue.get(), timeout=timeout)

    async def wait_for(
        self,
        predicate,
        *,
        collect: bool = False,
        timeout: float = 10.0,
    ) -> list[dict[str, Any]] | dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + timeout
        collected: list[dict[str, Any]] = []
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError(f"predicate never matched; saw={collected}")
            evt = await self.recv(timeout=remaining)
            collected.append(evt)
            if predicate(evt):
                return collected if collect else evt

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except (ConnectionError, OSError):
            pass
        self._task.cancel()
        try:
            await self._task
        except BaseException:
            pass


@pytest_asyncio.fixture
async def client_factory(daemon_and_socket):
    _daemon, socket_path = daemon_and_socket
    created: list[_StreamClient] = []

    async def make() -> _StreamClient:
        reader, writer = await asyncio.open_unix_connection(socket_path)
        c = _StreamClient(reader, writer)
        await c.send({"type": "blemeesd.hello", "client": "test/0", "protocol": PROTOCOL_VERSION})
        ack = await c.recv()
        assert ack["type"] == "blemeesd.hello_ack", ack
        created.append(c)
        return c

    yield make
    for c in created:
        await c.close()
