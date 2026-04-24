"""Daemon-level tests for mid-turn disconnect, replay, and durable logs."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import pytest_asyncio

from blemees import PROTOCOL_VERSION
from blemees.config import Config
from blemees.daemon import Daemon
from blemees.logging import configure

FAKE_CLAUDE = str(Path(__file__).parent / "fake_claude.py")


pytestmark = pytest.mark.asyncio


def _make_config(
    tmp_path: Path, *, event_log_dir: Path | None = None, ring_buffer_size: int = 1024
) -> Config:
    from tests.blemees.conftest import short_socket_path

    return Config(
        socket_path=str(short_socket_path("blemeesd-replay")),
        claude_bin=FAKE_CLAUDE,
        idle_timeout_s=60,
        max_concurrent_sessions=8,
        ring_buffer_size=ring_buffer_size,
        event_log_dir=str(event_log_dir) if event_log_dir else None,
    )


@pytest_asyncio.fixture
async def custom_daemon(tmp_path, monkeypatch, request):
    overrides = getattr(request, "param", None) or {}
    monkeypatch.setenv("BLEMEES_FAKE_MODE", overrides.get("fake_mode", "normal"))

    event_log_dir = overrides.get("event_log_dir")
    if event_log_dir == "__tmp__":
        event_log_dir = tmp_path / "event_log"
    cfg = _make_config(
        tmp_path,
        event_log_dir=event_log_dir,
        ring_buffer_size=overrides.get("ring_buffer_size", 1024),
    )
    logger = configure("error")
    daemon = Daemon(cfg, logger)
    await daemon.start()
    serve_task = asyncio.create_task(daemon.serve_forever())
    try:
        yield daemon, cfg
    finally:
        daemon.request_shutdown()
        try:
            await asyncio.wait_for(serve_task, timeout=5.0)
        except TimeoutError:
            serve_task.cancel()


class _Stream:
    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer
        self._queue: asyncio.Queue = asyncio.Queue()
        self._pump = asyncio.create_task(self._run())

    async def _run(self):
        try:
            while True:
                raw = await self.reader.readuntil(b"\n")
                await self._queue.put(json.loads(raw.rstrip(b"\r\n").decode("utf-8")))
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            await self._queue.put(None)

    async def send(self, frame):
        self.writer.write((json.dumps(frame) + "\n").encode("utf-8"))
        await self.writer.drain()

    async def recv(self, timeout=5.0):
        evt = await asyncio.wait_for(self._queue.get(), timeout=timeout)
        if evt is None:
            raise ConnectionError("connection closed")
        return evt

    async def wait_for(self, pred, *, timeout=10.0):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError("predicate never matched")
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


async def _connect(socket_path: str) -> _Stream:
    reader, writer = await asyncio.open_unix_connection(socket_path)
    s = _Stream(reader, writer)
    await s.send({"type": "blemeesd.hello", "client": "t/0", "protocol": PROTOCOL_VERSION})
    ack = await s.recv()
    assert ack["type"] == "blemeesd.hello_ack"
    return s


# ---------------------------------------------------------------------------
# Option 1: events carry seq and land in the ring buffer
# ---------------------------------------------------------------------------


async def test_outbound_events_carry_monotonic_seq(custom_daemon):
    _daemon, cfg = custom_daemon
    s = await _connect(cfg.socket_path)
    try:
        await s.send({"type": "blemeesd.open", "id": "r1", "session_id": "s1", "tools": ""})
        opened = await s.wait_for(lambda e: e["type"] == "blemeesd.opened")
        assert opened["last_seq"] == 0
        await s.send(
            {
                "type": "claude.user",
                "session_id": "s1",
                "message": {"role": "user", "content": "hi"},
            }
        )
        seqs: list[int] = []
        while True:
            evt = await s.recv(timeout=5.0)
            seq = evt.get("seq")
            if isinstance(seq, int):
                seqs.append(seq)
            if evt.get("type") == "claude.result":
                break
        assert seqs == sorted(seqs)
        assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))
        assert (
            len(seqs) >= 3
        )  # claude.system + claude.stream_event + claude.assistant + claude.result
    finally:
        await s.close()


# ---------------------------------------------------------------------------
# Option 2: reconnect with last_seen_seq replays missed frames
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "custom_daemon",
    [{"fake_mode": "normal"}],
    indirect=True,
)
async def test_reconnect_replays_from_last_seen_seq(custom_daemon):
    _daemon, cfg = custom_daemon

    # First connection opens the session and sends a turn; collect all seqs.
    s1 = await _connect(cfg.socket_path)
    await s1.send({"type": "blemeesd.open", "id": "r1", "session_id": "rep", "tools": ""})
    await s1.wait_for(lambda e: e["type"] == "blemeesd.opened")
    await s1.send(
        {"type": "claude.user", "session_id": "rep", "message": {"role": "user", "content": "hi"}}
    )
    first_seen: list[dict] = []
    while True:
        evt = await s1.recv(timeout=5.0)
        first_seen.append(evt)
        if evt.get("type") == "claude.result":
            break
    await s1.close()

    last_seq = max(e.get("seq", 0) for e in first_seen if isinstance(e.get("seq"), int))
    mid_seq = max(1, last_seq - 2)

    # Reconnect with last_seen_seq in the middle — we should get the tail.
    s2 = await _connect(cfg.socket_path)
    try:
        await s2.send(
            {
                "type": "blemeesd.open",
                "id": "r2",
                "session_id": "rep",
                "resume": True,
                "tools": "",
                "last_seen_seq": mid_seq,
            }
        )
        opened = await s2.wait_for(lambda e: e["type"] == "blemeesd.opened")
        assert opened["last_seq"] >= last_seq
        replayed: list[int] = []
        # Consume until we catch up (no more frames within 0.3s).
        while True:
            try:
                evt = await s2.recv(timeout=0.3)
            except TimeoutError:
                break
            seq = evt.get("seq")
            if isinstance(seq, int):
                replayed.append(seq)
        assert replayed, "expected some replayed frames"
        assert min(replayed) == mid_seq + 1
        assert max(replayed) == last_seq
    finally:
        await s2.close()


@pytest.mark.parametrize(
    "custom_daemon",
    [{"fake_mode": "normal", "ring_buffer_size": 2}],
    indirect=True,
)
async def test_reconnect_emits_replay_gap_when_buffer_rolled_over(custom_daemon):
    _daemon, cfg = custom_daemon

    s1 = await _connect(cfg.socket_path)
    await s1.send({"type": "blemeesd.open", "id": "r1", "session_id": "gap", "tools": ""})
    await s1.wait_for(lambda e: e["type"] == "blemeesd.opened")
    await s1.send(
        {"type": "claude.user", "session_id": "gap", "message": {"role": "user", "content": "hi"}}
    )
    await s1.wait_for(lambda e: e.get("type") == "claude.result")
    await s1.close()

    # With a tiny ring, most of the turn's events are gone from memory.
    s2 = await _connect(cfg.socket_path)
    try:
        await s2.send(
            {
                "type": "blemeesd.open",
                "id": "r2",
                "session_id": "gap",
                "resume": True,
                "tools": "",
                "last_seen_seq": 1,
            }
        )
        await s2.wait_for(lambda e: e["type"] == "blemeesd.opened")
        gap = await s2.wait_for(lambda e: e.get("type") == "blemeesd.replay_gap")
        assert gap["since_seq"] == 1
        assert gap["first_available_seq"] > 2
    finally:
        await s2.close()


# ---------------------------------------------------------------------------
# Option 1 again: mid-turn disconnect lets the subprocess run to completion
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "custom_daemon",
    [{"fake_mode": "normal"}],
    indirect=True,
)
async def test_mid_turn_disconnect_preserves_events_for_replay(custom_daemon):
    _daemon, cfg = custom_daemon

    # Open + issue a turn, then drop the connection before reading all events.
    s1 = await _connect(cfg.socket_path)
    await s1.send({"type": "blemeesd.open", "id": "r1", "session_id": "mid", "tools": ""})
    await s1.wait_for(lambda e: e["type"] == "blemeesd.opened")
    await s1.send(
        {"type": "claude.user", "session_id": "mid", "message": {"role": "user", "content": "hi"}}
    )
    await s1.wait_for(lambda e: e.get("type") == "claude.stream_event")
    # Drop without reading further.
    await s1.close()

    # Wait long enough for the fake claude to finish (it's fast) and Session
    # to buffer the full turn.
    await asyncio.sleep(0.5)

    # Reconnect and replay from seq 0.
    s2 = await _connect(cfg.socket_path)
    try:
        await s2.send(
            {
                "type": "blemeesd.open",
                "id": "r2",
                "session_id": "mid",
                "resume": True,
                "tools": "",
                "last_seen_seq": 0,
            }
        )
        await s2.wait_for(lambda e: e["type"] == "blemeesd.opened")
        saw_result = False
        while True:
            try:
                evt = await s2.recv(timeout=0.3)
            except TimeoutError:
                break
            if evt.get("type") == "claude.result" and evt.get("session_id") == "mid":
                saw_result = True
        assert saw_result, "result must have been buffered while disconnected"
    finally:
        await s2.close()


# ---------------------------------------------------------------------------
# Option 3: durable log survives daemon restart
# ---------------------------------------------------------------------------


async def test_event_log_survives_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("BLEMEES_FAKE_MODE", "normal")
    log_dir = tmp_path / "event_log"

    cfg = _make_config(tmp_path, event_log_dir=log_dir)
    logger = configure("error")

    # First daemon: open, send a turn, close.
    d1 = Daemon(cfg, logger)
    await d1.start()
    t1 = asyncio.create_task(d1.serve_forever())
    try:
        s = await _connect(cfg.socket_path)
        await s.send({"type": "blemeesd.open", "id": "r1", "session_id": "dur", "tools": ""})
        await s.wait_for(lambda e: e["type"] == "blemeesd.opened")
        await s.send(
            {
                "type": "claude.user",
                "session_id": "dur",
                "message": {"role": "user", "content": "hi"},
            }
        )
        await s.wait_for(lambda e: e.get("type") == "claude.result")
        await s.close()
    finally:
        d1.request_shutdown()
        await asyncio.wait_for(t1, timeout=5.0)

    # Log file exists.
    log_file = log_dir / "dur.jsonl"
    assert log_file.is_file()
    raw = log_file.read_text().strip().splitlines()
    seqs = [json.loads(line).get("seq") for line in raw if line.strip()]
    assert seqs == sorted(seqs)

    # Second daemon starts fresh, sees the log, can replay into a new client.
    from tests.blemees.conftest import short_socket_path

    cfg2 = _make_config(tmp_path, event_log_dir=log_dir)
    cfg2.socket_path = str(short_socket_path("blemeesd-replay2"))
    d2 = Daemon(cfg2, logger)
    await d2.start()
    t2 = asyncio.create_task(d2.serve_forever())
    try:
        s = await _connect(cfg2.socket_path)
        await s.send(
            {
                "type": "blemeesd.open",
                "id": "r1",
                "session_id": "dur",
                "resume": True,
                "tools": "",
                "last_seen_seq": 0,
            }
        )
        opened = await s.wait_for(lambda e: e["type"] == "blemeesd.opened")
        # last_seq reflects the prior daemon's seq.
        assert opened["last_seq"] >= max(seqs)
        replayed = 0
        while True:
            try:
                evt = await s.recv(timeout=0.3)
            except TimeoutError:
                break
            if isinstance(evt.get("seq"), int):
                replayed += 1
        assert replayed >= len(seqs)
        await s.close()
    finally:
        d2.request_shutdown()
        await asyncio.wait_for(t2, timeout=5.0)
