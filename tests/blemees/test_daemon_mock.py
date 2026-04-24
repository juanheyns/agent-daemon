"""Daemon-level tests using the fake claude stub.

These tests exercise the wire protocol, session lifecycle, interrupt,
crash-and-respawn, concurrent sessions, and unsafe flag rejection end-to-end
through a real Unix socket — without a real Claude binary.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from blemees import PROTOCOL_VERSION

pytestmark = pytest.mark.asyncio


async def test_hello_roundtrip(daemon_and_socket):
    _daemon, path = daemon_and_socket
    reader, writer = await asyncio.open_unix_connection(path)
    writer.write(
        (
            json.dumps({"type": "blemeesd.hello", "client": "t/0", "protocol": PROTOCOL_VERSION})
            + "\n"
        ).encode()
    )
    await writer.drain()
    ack = json.loads((await reader.readuntil(b"\n")).decode())
    assert ack["type"] == "blemeesd.hello_ack"
    assert ack["protocol"] == PROTOCOL_VERSION
    writer.close()
    await writer.wait_closed()


async def test_protocol_mismatch_closes_connection(daemon_and_socket):
    _daemon, path = daemon_and_socket
    reader, writer = await asyncio.open_unix_connection(path)
    writer.write((json.dumps({"type": "blemeesd.hello", "protocol": "blemees/99"}) + "\n").encode())
    await writer.drain()
    err = json.loads((await reader.readuntil(b"\n")).decode())
    assert err["type"] == "blemeesd.error"
    assert err["code"] == "protocol_mismatch"


async def test_open_then_user_then_result(client_factory, fake_mode):
    fake_mode("normal")
    client = await client_factory()
    await client.send({"type": "blemeesd.open", "id": "r1", "session_id": "s1", "tools": ""})
    opened = await client.wait_for(lambda e: e["type"] == "blemeesd.opened")
    assert opened["session_id"] == "s1"
    await client.send(
        {"type": "claude.user", "session_id": "s1", "message": {"role": "user", "content": "hello"}}
    )
    result = await client.wait_for(
        lambda e: e.get("type") == "claude.result" and e.get("session_id") == "s1"
    )
    assert result["subtype"] == "success"


async def test_unsafe_flag_rejected_on_open(client_factory):
    client = await client_factory()
    await client.send(
        {
            "type": "blemeesd.open",
            "id": "r1",
            "session_id": "s1",
            "dangerously_skip_permissions": True,
        }
    )
    err = await client.wait_for(lambda e: e.get("type") == "blemeesd.error")
    assert err["code"] == "unsafe_flag"


async def test_unknown_message_returns_error(client_factory):
    client = await client_factory()
    await client.send({"type": "blemeesd.nonsense", "session_id": "s1"})
    err = await client.wait_for(lambda e: e.get("type") == "blemeesd.error")
    assert err["code"] == "unknown_message"


async def test_ping_replies_with_pong(client_factory):
    client = await client_factory()
    await client.send({"type": "blemeesd.ping", "id": "ping-1", "data": 42})
    pong = await client.wait_for(lambda e: e.get("type") == "blemeesd.pong")
    assert pong["id"] == "ping-1"
    assert pong["data"] == 42


async def test_session_flag_mapping_in_argv(client_factory, fake_mode, argv_trace_path):
    fake_mode("normal")
    client = await client_factory()
    await client.send(
        {
            "type": "blemeesd.open",
            "id": "r1",
            "session_id": "s-map",
            "model": "sonnet",
            "tools": "",
            "permission_mode": "bypassPermissions",
        }
    )
    await client.wait_for(lambda e: e.get("type") == "blemeesd.opened")
    # Wait for the first CC event so we know fake_claude has recorded its argv.
    await client.wait_for(lambda e: e.get("type") == "claude.system")
    argv_lines = Path(argv_trace_path).read_text().strip().splitlines()
    assert argv_lines, "fake claude was not spawned"
    argv = json.loads(argv_lines[0])
    assert "--session-id" in argv
    assert argv[argv.index("--session-id") + 1] == "s-map"
    assert "--model" in argv and argv[argv.index("--model") + 1] == "sonnet"
    assert "--permission-mode" in argv
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
    assert "--tools" in argv
    # --verbose is required by spec §5.4 (required by CC for stream-json + -p)
    assert "--verbose" in argv


async def test_resume_flag_used_when_requested(client_factory, fake_mode, argv_trace_path):
    fake_mode("normal")
    client = await client_factory()
    await client.send(
        {
            "type": "blemeesd.open",
            "id": "r1",
            "session_id": "s-resume",
            "resume": True,
            "tools": "",
        }
    )
    await client.wait_for(lambda e: e.get("type") == "blemeesd.opened")
    await client.wait_for(lambda e: e.get("type") == "claude.system")
    argv = json.loads(Path(argv_trace_path).read_text().strip().splitlines()[0])
    assert "--resume" in argv
    assert "--session-id" not in argv


async def test_interrupt_respawns_with_resume(
    client_factory, fake_mode, argv_trace_path, monkeypatch
):
    fake_mode("slow")
    client = await client_factory()
    await client.send({"type": "blemeesd.open", "id": "r1", "session_id": "s-int", "tools": ""})
    await client.wait_for(lambda e: e.get("type") == "blemeesd.opened")
    await client.send(
        {"type": "claude.user", "session_id": "s-int", "message": {"role": "user", "content": "go"}}
    )
    # Wait until streaming starts.
    await client.wait_for(lambda e: e.get("type") == "claude.stream_event")
    await client.send({"type": "blemeesd.interrupt", "session_id": "s-int"})
    ir = await client.wait_for(lambda e: e.get("type") == "blemeesd.interrupted")
    assert ir["was_idle"] is False

    # After interrupt the argv trace should contain a second line using --resume.
    # Give the daemon a moment to finish respawn.
    for _ in range(30):
        lines = Path(argv_trace_path).read_text().strip().splitlines()
        if len(lines) >= 2:
            break
        await asyncio.sleep(0.05)
    lines = Path(argv_trace_path).read_text().strip().splitlines()
    assert len(lines) >= 2
    resumed_argv = json.loads(lines[1])
    assert "--resume" in resumed_argv
    assert resumed_argv[resumed_argv.index("--resume") + 1] == "s-int"


async def test_concurrent_sessions_dont_interfere(client_factory, fake_mode):
    fake_mode("normal")
    client = await client_factory()
    for sid in ("a", "b", "c"):
        await client.send(
            {"type": "blemeesd.open", "id": f"r{sid}", "session_id": sid, "tools": ""}
        )
    opens = set()
    while len(opens) < 3:
        evt = await client.recv(timeout=5.0)
        if evt.get("type") == "blemeesd.opened":
            opens.add(evt["session_id"])

    for sid in ("a", "b", "c"):
        await client.send(
            {
                "type": "claude.user",
                "session_id": sid,
                "message": {"role": "user", "content": f"hi-{sid}"},
            }
        )

    saw: dict[str, bool] = {}
    while len(saw) < 3:
        evt = await client.recv(timeout=5.0)
        if evt.get("type") == "claude.result" and evt.get("session_id") in {"a", "b", "c"}:
            saw[evt["session_id"]] = True
    assert set(saw) == {"a", "b", "c"}


async def test_crash_mid_turn_then_recover(client_factory, fake_mode, monkeypatch):
    fake_mode("crash")
    client = await client_factory()
    await client.send({"type": "blemeesd.open", "id": "r1", "session_id": "s-crash", "tools": ""})
    await client.wait_for(lambda e: e.get("type") == "blemeesd.opened")
    await client.send(
        {
            "type": "claude.user",
            "session_id": "s-crash",
            "message": {"role": "user", "content": "boom"},
        }
    )
    err = await client.wait_for(
        lambda e: e.get("type") == "blemeesd.error" and e.get("code") == "claude_crashed"
    )
    assert err["session_id"] == "s-crash"

    # Next user message should transparently respawn (spec §9.1).
    fake_mode("normal")
    await client.send(
        {
            "type": "claude.user",
            "session_id": "s-crash",
            "message": {"role": "user", "content": "again"},
        }
    )
    await client.wait_for(
        lambda e: e.get("type") == "claude.result" and e.get("session_id") == "s-crash"
    )


async def test_close_removes_session(client_factory, fake_mode):
    fake_mode("normal")
    client = await client_factory()
    await client.send({"type": "blemeesd.open", "id": "r1", "session_id": "s-close", "tools": ""})
    await client.wait_for(lambda e: e.get("type") == "blemeesd.opened")
    await client.send(
        {"type": "blemeesd.close", "id": "r2", "session_id": "s-close", "delete": False}
    )
    closed = await client.wait_for(lambda e: e.get("type") == "blemeesd.closed")
    assert closed["session_id"] == "s-close"

    # Re-open without resume should succeed since session is gone.
    await client.send({"type": "blemeesd.open", "id": "r3", "session_id": "s-close", "tools": ""})
    await client.wait_for(lambda e: e.get("type") == "blemeesd.opened")


async def test_detach_allows_reattach_via_resume(client_factory, fake_mode):
    fake_mode("normal")
    c1 = await client_factory()
    await c1.send({"type": "blemeesd.open", "id": "r1", "session_id": "s-det", "tools": ""})
    await c1.wait_for(lambda e: e.get("type") == "blemeesd.opened")
    await c1.close()

    # New connection can reattach via resume:true.
    c2 = await client_factory()
    await c2.send(
        {
            "type": "blemeesd.open",
            "id": "r2",
            "session_id": "s-det",
            "resume": True,
            "tools": "",
        }
    )
    await c2.wait_for(lambda e: e.get("type") == "blemeesd.opened")


async def test_invalid_message_keeps_connection_alive(client_factory):
    client = await client_factory()
    # Send a malformed open (no session).
    await client.send({"type": "blemeesd.open", "id": "bad"})
    err = await client.wait_for(lambda e: e.get("type") == "blemeesd.error")
    assert err["code"] == "invalid_message"
    # Connection still usable: a truly unknown type still returns unknown.
    await client.send({"type": "blemeesd.nonsense_xyz"})
    err2 = await client.wait_for(lambda e: e.get("type") == "blemeesd.error")
    assert err2["code"] == "unknown_message"


# ---------------------------------------------------------------------------
# list_sessions
# ---------------------------------------------------------------------------


def _seed_transcript(
    home_dir: Path, cwd: str, session_id: str, preview: str | None, mtime: int
) -> Path:
    from blemees.subprocess import project_dir_for_cwd as _pdfc

    # Recompute project dir against the fake $HOME so the test matches the
    # runtime encoding.
    old_home = os.environ.get("HOME")
    os.environ["HOME"] = str(home_dir)
    try:
        project_dir = _pdfc(cwd)
    finally:
        if old_home is None:
            del os.environ["HOME"]
        else:
            os.environ["HOME"] = old_home
    project_dir.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"type": "system", "subtype": "init"})]
    if preview is not None:
        lines.append(
            json.dumps(
                {
                    "type": "user",
                    "message": {"role": "user", "content": preview},
                }
            )
        )
    path = project_dir / f"{session_id}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


async def test_list_sessions_requires_cwd(client_factory):
    client = await client_factory()
    await client.send({"type": "blemeesd.list_sessions", "id": "r1"})
    err = await client.wait_for(lambda e: e.get("type") == "blemeesd.error")
    assert err["code"] == "invalid_message"


async def test_list_sessions_empty_for_unknown_cwd(client_factory, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    client = await client_factory()
    await client.send({"type": "blemeesd.list_sessions", "id": "r1", "cwd": "/nope/nope"})
    reply = await client.wait_for(lambda e: e.get("type") == "blemeesd.sessions")
    assert reply["id"] == "r1"
    assert reply["cwd"] == "/nope/nope"
    assert reply["sessions"] == []


async def test_list_sessions_returns_sorted_on_disk(client_factory, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cwd = "/home/u/proj"
    _seed_transcript(tmp_path, cwd, "older", "old one", 1_700_000_000)
    _seed_transcript(tmp_path, cwd, "newer", "new one", 1_700_000_100)

    client = await client_factory()
    await client.send({"type": "blemeesd.list_sessions", "id": "r1", "cwd": cwd})
    reply = await client.wait_for(lambda e: e.get("type") == "blemeesd.sessions")
    ids = [s["session_id"] for s in reply["sessions"]]
    assert ids == ["newer", "older"]
    assert reply["sessions"][0]["preview"] == "new one"
    assert reply["sessions"][0]["attached"] is False
    assert reply["sessions"][0]["mtime_ms"] == 1_700_000_100_000


async def test_list_sessions_flags_attached(client_factory, fake_mode, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    fake_mode("normal")
    cwd = str(tmp_path)  # any unique, existing dir
    client = await client_factory()
    await client.send(
        {
            "type": "blemeesd.open",
            "id": "r1",
            "session_id": "live",
            "tools": "",
            "cwd": cwd,
        }
    )
    await client.wait_for(lambda e: e.get("type") == "blemeesd.opened")

    await client.send({"type": "blemeesd.list_sessions", "id": "r2", "cwd": cwd})
    reply = await client.wait_for(lambda e: e.get("type") == "blemeesd.sessions")
    records = {s["session_id"]: s for s in reply["sessions"]}
    assert "live" in records
    assert records["live"]["attached"] is True


# ---------------------------------------------------------------------------
# Session takeover
# ---------------------------------------------------------------------------


async def test_takeover_notifies_previous_owner(client_factory, fake_mode):
    fake_mode("normal")
    a = await client_factory()
    b = await client_factory()

    await a.send({"type": "blemeesd.open", "id": "r1", "session_id": "shared", "tools": ""})
    await a.wait_for(lambda e: e.get("type") == "blemeesd.opened")

    # B takes over via resume=true.
    await b.send(
        {
            "type": "blemeesd.open",
            "id": "r2",
            "session_id": "shared",
            "resume": True,
            "tools": "",
        }
    )

    # A must see the notification.
    notice = await a.wait_for(
        lambda e: e.get("type") == "blemeesd.session_taken" and e.get("session_id") == "shared"
    )
    # Informational peer_pid may be absent in tests (no SO_PEERCRED capture
    # for in-process unix sockets on some kernels), but the frame must arrive.
    assert notice["session_id"] == "shared"

    # B's ack arrives and the event stream now flows to B.
    await b.wait_for(lambda e: e.get("type") == "blemeesd.opened")
    await b.send(
        {
            "type": "claude.user",
            "session_id": "shared",
            "message": {"role": "user", "content": "hi"},
        }
    )
    await b.wait_for(lambda e: e.get("type") == "claude.result" and e.get("session_id") == "shared")


async def test_no_takeover_notice_for_same_connection_reopen(client_factory, fake_mode):
    fake_mode("normal")
    c = await client_factory()
    await c.send({"type": "blemeesd.open", "id": "r1", "session_id": "self", "tools": ""})
    await c.wait_for(lambda e: e.get("type") == "blemeesd.opened")

    # Reopen from the same connection with resume=true — no takeover.
    await c.send(
        {
            "type": "blemeesd.open",
            "id": "r2",
            "session_id": "self",
            "resume": True,
            "tools": "",
        }
    )
    # The opened ack arrives; no session_taken in between.
    collected = await c.wait_for(
        lambda e: e.get("id") == "r2" and e.get("type") == "blemeesd.opened",
        collect=True,
    )
    assert not any(e.get("type") == "blemeesd.session_taken" for e in collected)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


async def test_status_returns_snapshot(client_factory, fake_mode):
    fake_mode("normal")
    client = await client_factory()
    await client.send({"type": "blemeesd.status", "id": "s-1"})
    reply = await client.wait_for(lambda e: e.get("type") == "blemeesd.status_reply")
    assert reply["id"] == "s-1"
    assert reply["protocol"] == "blemees/1"
    assert reply["daemon"].startswith("blemeesd/")
    assert reply["pid"] > 0
    assert reply["uptime_s"] >= 0.0
    assert reply["connections"] >= 1
    assert reply["sessions"] == {
        "total": 0,
        "attached": 0,
        "detached": 0,
        "active_turns": 0,
    }
    cfg = reply["config"]
    assert cfg["ring_buffer_size"] > 0
    assert "shutdown_grace_s" in cfg


async def test_status_reflects_open_sessions(client_factory, fake_mode):
    fake_mode("normal")
    client = await client_factory()
    await client.send({"type": "blemeesd.open", "id": "r1", "session_id": "x", "tools": ""})
    await client.wait_for(lambda e: e.get("type") == "blemeesd.opened")
    await client.send({"type": "blemeesd.status"})
    reply = await client.wait_for(lambda e: e.get("type") == "blemeesd.status_reply")
    assert reply["sessions"]["total"] == 1
    assert reply["sessions"]["attached"] == 1


# ---------------------------------------------------------------------------
# watch / unwatch
# ---------------------------------------------------------------------------


async def test_watch_receives_events_live(client_factory, fake_mode):
    fake_mode("normal")
    owner = await client_factory()
    watcher = await client_factory()

    await owner.send({"type": "blemeesd.open", "id": "r1", "session_id": "shared", "tools": ""})
    await owner.wait_for(lambda e: e.get("type") == "blemeesd.opened")

    await watcher.send({"type": "blemeesd.watch", "id": "w1", "session_id": "shared"})
    ack = await watcher.wait_for(lambda e: e.get("type") == "blemeesd.watching")
    assert ack["session_id"] == "shared"

    # Drive a turn on the owner; the watcher should see the same event stream.
    await owner.send(
        {
            "type": "claude.user",
            "session_id": "shared",
            "message": {"role": "user", "content": "hi"},
        }
    )
    await watcher.wait_for(
        lambda e: e.get("type") == "claude.result" and e.get("session_id") == "shared"
    )


async def test_watch_replays_from_last_seen_seq(client_factory, fake_mode):
    fake_mode("normal")
    owner = await client_factory()
    await owner.send({"type": "blemeesd.open", "id": "r1", "session_id": "rep", "tools": ""})
    await owner.wait_for(lambda e: e.get("type") == "blemeesd.opened")
    await owner.send(
        {
            "type": "claude.user",
            "session_id": "rep",
            "message": {"role": "user", "content": "hi"},
        }
    )
    await owner.wait_for(
        lambda e: e.get("type") == "claude.result" and e.get("session_id") == "rep"
    )

    watcher = await client_factory()
    await watcher.send(
        {
            "type": "blemeesd.watch",
            "id": "w1",
            "session_id": "rep",
            "last_seen_seq": 0,
        }
    )
    await watcher.wait_for(lambda e: e.get("type") == "blemeesd.watching")
    # Should catch up through the replay and see the completed turn.
    await watcher.wait_for(
        lambda e: e.get("type") == "claude.result" and e.get("session_id") == "rep"
    )


async def test_unwatch_stops_delivery(client_factory, fake_mode):
    fake_mode("normal")
    owner = await client_factory()
    watcher = await client_factory()
    await owner.send({"type": "blemeesd.open", "id": "r1", "session_id": "u", "tools": ""})
    await owner.wait_for(lambda e: e.get("type") == "blemeesd.opened")
    await watcher.send({"type": "blemeesd.watch", "id": "w1", "session_id": "u"})
    await watcher.wait_for(lambda e: e.get("type") == "blemeesd.watching")

    await watcher.send({"type": "blemeesd.unwatch", "id": "u1", "session_id": "u"})
    ack = await watcher.wait_for(lambda e: e.get("type") == "blemeesd.unwatched")
    assert ack["was_watching"] is True

    # Now drive a turn; the watcher should NOT see claude.result.
    await owner.send(
        {
            "type": "claude.user",
            "session_id": "u",
            "message": {"role": "user", "content": "go"},
        }
    )
    await owner.wait_for(lambda e: e.get("type") == "claude.result" and e.get("session_id") == "u")
    # Give event propagation a beat, then confirm the watcher queue is idle.
    await asyncio.sleep(0.1)
    try:
        evt = await watcher.recv(timeout=0.2)
        assert evt.get("type") != "claude.result", f"unexpected frame {evt}"
    except TimeoutError:
        pass


async def test_watch_unknown_session_errors(client_factory):
    client = await client_factory()
    await client.send({"type": "blemeesd.watch", "id": "w1", "session_id": "ghost"})
    err = await client.wait_for(lambda e: e.get("type") == "blemeesd.error")
    assert err["code"] == "session_unknown"


# ---------------------------------------------------------------------------
# session_info
# ---------------------------------------------------------------------------


async def test_session_info_unknown_session_errors(client_factory):
    client = await client_factory()
    await client.send({"type": "blemeesd.session_info", "id": "i1", "session_id": "nope"})
    err = await client.wait_for(lambda e: e.get("type") == "blemeesd.error")
    assert err["code"] == "session_unknown"


async def test_session_info_accumulates_across_turns(client_factory, fake_mode):
    fake_mode("normal")
    c = await client_factory()
    await c.send({"type": "blemeesd.open", "id": "r1", "session_id": "u", "tools": ""})
    await c.wait_for(lambda e: e.get("type") == "blemeesd.opened")

    # Zero counters before any turn.
    await c.send({"type": "blemeesd.session_info", "id": "i0", "session_id": "u"})
    zero = await c.wait_for(lambda e: e.get("type") == "blemeesd.session_info_reply")
    assert zero["turns"] == 0
    assert zero["cumulative_usage"]["input_tokens"] == 0

    # One turn (fake emits usage: in=10, out=5).
    for i in range(3):
        await c.send(
            {
                "type": "claude.user",
                "session_id": "u",
                "message": {"role": "user", "content": f"hi {i}"},
            }
        )
        await c.wait_for(lambda e: e.get("type") == "claude.result" and e.get("session_id") == "u")

    await c.send({"type": "blemeesd.session_info", "id": "i1", "session_id": "u"})
    info = await c.wait_for(lambda e: e.get("type") == "blemeesd.session_info_reply")
    assert info["turns"] == 3
    assert info["cumulative_usage"]["input_tokens"] == 30
    assert info["cumulative_usage"]["output_tokens"] == 15
    assert info["last_turn_usage"]["input_tokens"] == 10
    assert info["model"] == "claude-fake"
    assert info["attached"] is True
    assert info["subprocess_running"] is True
    assert info["last_seq"] > 0


async def test_session_info_survives_daemon_restart(tmp_path, monkeypatch):
    """Usage persists across a daemon restart when event_log_dir is set."""
    from blemees import PROTOCOL_VERSION
    from blemees.config import Config
    from blemees.daemon import Daemon
    from blemees.logging import configure

    fake = str(Path(__file__).parent / "fake_claude.py")
    log_dir = tmp_path / "events"
    monkeypatch.setenv("BLEMEES_FAKE_MODE", "normal")

    def _cfg(sock: Path) -> Config:
        return Config(
            socket_path=str(sock),
            claude_bin=fake,
            idle_timeout_s=60,
            max_concurrent_sessions=8,
            event_log_dir=str(log_dir),
        )

    async def _connect(path: Path):
        r, w = await asyncio.open_unix_connection(str(path))

        async def send(frame):
            w.write((json.dumps(frame) + "\n").encode())
            await w.drain()

        async def recv():
            raw = await r.readuntil(b"\n")
            return json.loads(raw.rstrip(b"\r\n").decode("utf-8"))

        async def wait_for(pred, timeout=5.0):
            deadline = asyncio.get_running_loop().time() + timeout
            while True:
                if asyncio.get_running_loop().time() > deadline:
                    raise TimeoutError
                evt = await asyncio.wait_for(recv(), timeout=1.5)
                if pred(evt):
                    return evt

        await send(
            {
                "type": "blemeesd.hello",
                "client": "t/0",
                "protocol": PROTOCOL_VERSION,
            }
        )
        await recv()  # hello_ack
        return w, send, wait_for

    from tests.blemees.conftest import short_socket_path

    # ----- first daemon: run two turns, confirm counters, shut down.
    sock1 = short_socket_path("blemeesd-persist1")
    cfg1 = _cfg(sock1)
    d1 = Daemon(cfg1, configure("error"))
    await d1.start()
    t1 = asyncio.create_task(d1.serve_forever())
    try:
        w, send, wait_for = await _connect(sock1)
        await send({"type": "blemeesd.open", "id": "r1", "session_id": "keep", "tools": ""})
        await wait_for(lambda e: e.get("type") == "blemeesd.opened")
        for _ in range(2):
            await send(
                {
                    "type": "claude.user",
                    "session_id": "keep",
                    "message": {"role": "user", "content": "hi"},
                }
            )
            await wait_for(
                lambda e: e.get("type") == "claude.result" and e.get("session_id") == "keep"
            )
        w.close()
        try:
            await w.wait_closed()
        except Exception:
            pass
    finally:
        d1.request_shutdown()
        await asyncio.wait_for(t1, timeout=5.0)

    # Sidecar is on disk.
    sidecar = log_dir / "keep.usage.json"
    assert sidecar.is_file()

    # ----- second daemon: reopen resume=true, query info, expect 2 turns.
    sock2 = short_socket_path("blemeesd-persist2")
    cfg2 = _cfg(sock2)
    d2 = Daemon(cfg2, configure("error"))
    await d2.start()
    t2 = asyncio.create_task(d2.serve_forever())
    try:
        w, send, wait_for = await _connect(sock2)
        await send(
            {
                "type": "blemeesd.open",
                "id": "r2",
                "session_id": "keep",
                "resume": True,
                "tools": "",
            }
        )
        await wait_for(lambda e: e.get("type") == "blemeesd.opened")
        await send({"type": "blemeesd.session_info", "id": "i1", "session_id": "keep"})
        info = await wait_for(lambda e: e.get("type") == "blemeesd.session_info_reply")
        assert info["turns"] == 2
        assert info["cumulative_usage"]["input_tokens"] == 20
        assert info["cumulative_usage"]["output_tokens"] == 10
        w.close()
        try:
            await w.wait_closed()
        except Exception:
            pass
    finally:
        d2.request_shutdown()
        await asyncio.wait_for(t2, timeout=5.0)
