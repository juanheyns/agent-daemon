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
            json.dumps(
                {"type": "blemeesd.hello", "client": "t/0", "protocol": PROTOCOL_VERSION}
            )
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
    writer.write(
        (json.dumps({"type": "blemeesd.hello", "protocol": "blemees/99"}) + "\n").encode()
    )
    await writer.drain()
    err = json.loads((await reader.readuntil(b"\n")).decode())
    assert err["type"] == "blemeesd.error"
    assert err["code"] == "protocol_mismatch"


async def test_open_then_user_then_result(client_factory, fake_mode):
    fake_mode("normal")
    client = await client_factory()
    await client.send(
        {"type": "blemeesd.open", "id": "r1", "session": "s1", "tools": ""}
    )
    opened = await client.wait_for(lambda e: e["type"] == "blemeesd.opened")
    assert opened["session"] == "s1"
    await client.send({"type": "claude.user", "session": "s1", "text": "hello"})
    result = await client.wait_for(
        lambda e: e.get("type") == "claude.result" and e.get("session") == "s1"
    )
    assert result["subtype"] == "success"


async def test_unsafe_flag_rejected_on_open(client_factory):
    client = await client_factory()
    await client.send(
        {
            "type": "blemeesd.open",
            "id": "r1",
            "session": "s1",
            "dangerously_skip_permissions": True,
        }
    )
    err = await client.wait_for(lambda e: e.get("type") == "blemeesd.error")
    assert err["code"] == "unsafe_flag"


async def test_unknown_message_returns_error(client_factory):
    client = await client_factory()
    await client.send({"type": "blemeesd.nonsense", "session": "s1"})
    err = await client.wait_for(lambda e: e.get("type") == "blemeesd.error")
    assert err["code"] == "unknown_message"


async def test_reserved_types_return_unknown(client_factory):
    client = await client_factory()
    await client.send({"type": "blemeesd.ping"})
    err = await client.wait_for(lambda e: e.get("type") == "blemeesd.error")
    assert err["code"] == "unknown_message"


async def test_session_flag_mapping_in_argv(
    client_factory, fake_mode, argv_trace_path
):
    fake_mode("normal")
    client = await client_factory()
    await client.send(
        {
            "type": "blemeesd.open",
            "id": "r1",
            "session": "s-map",
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


async def test_resume_flag_used_when_requested(
    client_factory, fake_mode, argv_trace_path
):
    fake_mode("normal")
    client = await client_factory()
    await client.send(
        {
            "type": "blemeesd.open",
            "id": "r1",
            "session": "s-resume",
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
    await client.send(
        {"type": "blemeesd.open", "id": "r1", "session": "s-int", "tools": ""}
    )
    await client.wait_for(lambda e: e.get("type") == "blemeesd.opened")
    await client.send({"type": "claude.user", "session": "s-int", "text": "go"})
    # Wait until streaming starts.
    await client.wait_for(lambda e: e.get("type") == "claude.stream_event")
    await client.send({"type": "blemeesd.interrupt", "session": "s-int"})
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
            {"type": "blemeesd.open", "id": f"r{sid}", "session": sid, "tools": ""}
        )
    opens = set()
    while len(opens) < 3:
        evt = await client.recv(timeout=5.0)
        if evt.get("type") == "blemeesd.opened":
            opens.add(evt["session"])

    for sid in ("a", "b", "c"):
        await client.send({"type": "claude.user", "session": sid, "text": f"hi-{sid}"})

    saw: dict[str, bool] = {}
    while len(saw) < 3:
        evt = await client.recv(timeout=5.0)
        if evt.get("type") == "claude.result" and evt.get("session") in {"a", "b", "c"}:
            saw[evt["session"]] = True
    assert set(saw) == {"a", "b", "c"}


async def test_crash_mid_turn_then_recover(
    client_factory, fake_mode, monkeypatch
):
    fake_mode("crash")
    client = await client_factory()
    await client.send(
        {"type": "blemeesd.open", "id": "r1", "session": "s-crash", "tools": ""}
    )
    await client.wait_for(lambda e: e.get("type") == "blemeesd.opened")
    await client.send({"type": "claude.user", "session": "s-crash", "text": "boom"})
    err = await client.wait_for(
        lambda e: e.get("type") == "blemeesd.error" and e.get("code") == "claude_crashed"
    )
    assert err["session"] == "s-crash"

    # Next user message should transparently respawn (spec §9.1).
    fake_mode("normal")
    await client.send({"type": "claude.user", "session": "s-crash", "text": "again"})
    await client.wait_for(
        lambda e: e.get("type") == "claude.result" and e.get("session") == "s-crash"
    )


async def test_close_removes_session(client_factory, fake_mode):
    fake_mode("normal")
    client = await client_factory()
    await client.send(
        {"type": "blemeesd.open", "id": "r1", "session": "s-close", "tools": ""}
    )
    await client.wait_for(lambda e: e.get("type") == "blemeesd.opened")
    await client.send(
        {"type": "blemeesd.close", "id": "r2", "session": "s-close", "delete": False}
    )
    closed = await client.wait_for(lambda e: e.get("type") == "blemeesd.closed")
    assert closed["session"] == "s-close"

    # Re-open without resume should succeed since session is gone.
    await client.send(
        {"type": "blemeesd.open", "id": "r3", "session": "s-close", "tools": ""}
    )
    await client.wait_for(lambda e: e.get("type") == "blemeesd.opened")


async def test_detach_allows_reattach_via_resume(client_factory, fake_mode):
    fake_mode("normal")
    c1 = await client_factory()
    await c1.send(
        {"type": "blemeesd.open", "id": "r1", "session": "s-det", "tools": ""}
    )
    await c1.wait_for(lambda e: e.get("type") == "blemeesd.opened")
    await c1.close()

    # New connection can reattach via resume:true.
    c2 = await client_factory()
    await c2.send(
        {
            "type": "blemeesd.open",
            "id": "r2",
            "session": "s-det",
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
    # Connection still usable.
    await client.send({"type": "blemeesd.ping"})
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
    await client.send(
        {"type": "blemeesd.list_sessions", "id": "r1", "cwd": "/nope/nope"}
    )
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
    ids = [s["session"] for s in reply["sessions"]]
    assert ids == ["newer", "older"]
    assert reply["sessions"][0]["preview"] == "new one"
    assert reply["sessions"][0]["attached"] is False
    assert reply["sessions"][0]["mtime_ms"] == 1_700_000_100_000


async def test_list_sessions_flags_attached(
    client_factory, fake_mode, tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    fake_mode("normal")
    cwd = str(tmp_path)  # any unique, existing dir
    client = await client_factory()
    await client.send(
        {
            "type": "blemeesd.open",
            "id": "r1",
            "session": "live",
            "tools": "",
            "cwd": cwd,
        }
    )
    await client.wait_for(lambda e: e.get("type") == "blemeesd.opened")

    await client.send({"type": "blemeesd.list_sessions", "id": "r2", "cwd": cwd})
    reply = await client.wait_for(lambda e: e.get("type") == "blemeesd.sessions")
    records = {s["session"]: s for s in reply["sessions"]}
    assert "live" in records
    assert records["live"]["attached"] is True
