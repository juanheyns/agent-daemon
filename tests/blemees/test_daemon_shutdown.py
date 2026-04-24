"""Tests for daemon shutdown behaviour: graceful (§5.9 soft-detach applied
to every session) vs force-kill when the grace period expires."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from blemees import PROTOCOL_VERSION
from blemees.config import Config
from blemees.daemon import Daemon
from blemees.logging import configure

FAKE_CLAUDE = str(Path(__file__).parent / "fake_claude.py")

pytestmark = pytest.mark.asyncio


def _config(tmp_path: Path, *, grace_s: int) -> Config:
    from tests.blemees.conftest import short_socket_path

    return Config(
        socket_path=str(short_socket_path("blemeesd-shutdown")),
        claude_bin=FAKE_CLAUDE,
        idle_timeout_s=60,
        max_concurrent_sessions=8,
        shutdown_grace_s=grace_s,
    )


class _Stream:
    def __init__(self, r, w):
        self.reader = r
        self.writer = w
        self._q: asyncio.Queue = asyncio.Queue()
        self._pump = asyncio.create_task(self._run())

    async def _run(self):
        try:
            while True:
                raw = await self.reader.readuntil(b"\n")
                await self._q.put(json.loads(raw.rstrip(b"\r\n").decode("utf-8")))
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            await self._q.put(None)

    async def send(self, frame):
        self.writer.write((json.dumps(frame) + "\n").encode())
        await self.writer.drain()

    async def recv(self, timeout=5.0):
        evt = await asyncio.wait_for(self._q.get(), timeout=timeout)
        if evt is None:
            raise ConnectionError("closed")
        return evt

    async def wait_for(self, pred, *, timeout=5.0):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError
            evt = await self.recv(timeout=remaining)
            if pred(evt):
                return evt

    async def close(self):
        self._pump.cancel()
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except (ConnectionError, OSError):
            pass


async def _connect(path: str) -> _Stream:
    r, w = await asyncio.open_unix_connection(path)
    s = _Stream(r, w)
    await s.send({"type": "blemeesd.hello", "client": "t/0", "protocol": PROTOCOL_VERSION})
    await s.recv()
    return s


async def _start_daemon(cfg: Config) -> tuple[Daemon, asyncio.Task]:
    daemon = Daemon(cfg, configure("error"))
    await daemon.start()
    task = asyncio.create_task(daemon.serve_forever())
    return daemon, task


# ---------------------------------------------------------------------------
# Graceful path: subprocess finishes during the grace period.
# ---------------------------------------------------------------------------


async def test_shutdown_waits_for_in_flight_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("BLEMEES_FAKE_MODE", "finish")
    monkeypatch.setenv("BLEMEES_FAKE_FINISH_DELAY_S", "0.4")
    cfg = _config(tmp_path, grace_s=5)
    daemon, task = await _start_daemon(cfg)
    try:
        s = await _connect(cfg.socket_path)
        try:
            await s.send({"type": "blemeesd.open", "id": "r1", "session_id": "g", "tools": ""})
            await s.wait_for(lambda e: e.get("type") == "blemeesd.opened")
            await s.send(
                {
                    "type": "claude.user",
                    "session_id": "g",
                    "message": {"role": "user", "content": "hi"},
                }
            )
            # Turn is now active; wait for first event so we're past spawn.
            await s.wait_for(lambda e: e.get("type") == "claude.stream_event")

            t0 = time.monotonic()
            daemon.request_shutdown()
            await asyncio.wait_for(task, timeout=10.0)
            elapsed = time.monotonic() - t0
        finally:
            await s.close()
        # The subprocess needed ~0.4 s to emit its result. A clean graceful
        # shutdown should take at least that long but much less than the
        # configured 5 s grace budget.
        assert 0.3 <= elapsed <= 3.0, f"elapsed={elapsed}"
    finally:
        if not task.done():
            daemon.request_shutdown()
            await asyncio.wait_for(task, timeout=5.0)


# ---------------------------------------------------------------------------
# Expiry path: subprocess never finishes; grace expires; force-kill.
# ---------------------------------------------------------------------------


async def test_shutdown_force_kills_when_grace_expires(tmp_path, monkeypatch):
    monkeypatch.setenv("BLEMEES_FAKE_MODE", "slow")
    cfg = _config(tmp_path, grace_s=1)
    daemon, task = await _start_daemon(cfg)
    try:
        s = await _connect(cfg.socket_path)
        try:
            await s.send({"type": "blemeesd.open", "id": "r1", "session_id": "slow", "tools": ""})
            await s.wait_for(lambda e: e.get("type") == "blemeesd.opened")
            await s.send(
                {
                    "type": "claude.user",
                    "session_id": "slow",
                    "message": {"role": "user", "content": "go"},
                }
            )
            await s.wait_for(lambda e: e.get("type") == "claude.stream_event")

            t0 = time.monotonic()
            daemon.request_shutdown()
            await asyncio.wait_for(task, timeout=10.0)
            elapsed = time.monotonic() - t0
        finally:
            await s.close()
        # Grace is 1 s; force-kill phase adds up to ~1 s more.
        assert 0.9 <= elapsed <= 4.0, f"elapsed={elapsed}"
    finally:
        if not task.done():
            daemon.request_shutdown()
            await asyncio.wait_for(task, timeout=5.0)


# ---------------------------------------------------------------------------
# Zero grace: no wait, immediate SIGTERM (legacy v0.1 behaviour).
# ---------------------------------------------------------------------------


async def test_shutdown_grace_zero_kills_immediately(tmp_path, monkeypatch):
    monkeypatch.setenv("BLEMEES_FAKE_MODE", "slow")
    cfg = _config(tmp_path, grace_s=0)
    daemon, task = await _start_daemon(cfg)
    try:
        s = await _connect(cfg.socket_path)
        try:
            await s.send({"type": "blemeesd.open", "id": "r1", "session_id": "z", "tools": ""})
            await s.wait_for(lambda e: e.get("type") == "blemeesd.opened")
            await s.send(
                {
                    "type": "claude.user",
                    "session_id": "z",
                    "message": {"role": "user", "content": "go"},
                }
            )
            await s.wait_for(lambda e: e.get("type") == "claude.stream_event")

            t0 = time.monotonic()
            daemon.request_shutdown()
            await asyncio.wait_for(task, timeout=5.0)
            elapsed = time.monotonic() - t0
        finally:
            await s.close()
        assert elapsed <= 2.0, f"elapsed={elapsed}"
    finally:
        if not task.done():
            daemon.request_shutdown()
            await asyncio.wait_for(task, timeout=5.0)


# ---------------------------------------------------------------------------
# Idle sessions (no turn in flight) are torn down immediately even at high grace.
# ---------------------------------------------------------------------------


async def test_shutdown_skips_wait_for_idle_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv("BLEMEES_FAKE_MODE", "normal")
    cfg = _config(tmp_path, grace_s=30)
    daemon, task = await _start_daemon(cfg)
    try:
        s = await _connect(cfg.socket_path)
        try:
            await s.send({"type": "blemeesd.open", "id": "r1", "session_id": "idle", "tools": ""})
            await s.wait_for(lambda e: e.get("type") == "blemeesd.opened")
            # No user turn sent; session is idle.
            t0 = time.monotonic()
            daemon.request_shutdown()
            await asyncio.wait_for(task, timeout=5.0)
            elapsed = time.monotonic() - t0
        finally:
            await s.close()
        # With no active turn, the 30 s grace should not apply.
        assert elapsed <= 2.0, f"elapsed={elapsed}"
    finally:
        if not task.done():
            daemon.request_shutdown()
            await asyncio.wait_for(task, timeout=5.0)
