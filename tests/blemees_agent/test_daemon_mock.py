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

from blemees_agent import PROTOCOL_VERSION

pytestmark = pytest.mark.asyncio


async def test_hello_roundtrip(daemon_and_socket):
    _daemon, path = daemon_and_socket
    reader, writer = await asyncio.open_unix_connection(path)
    writer.write(
        (
            json.dumps({"type": "agent.hello", "client": "t/0", "protocol": PROTOCOL_VERSION})
            + "\n"
        ).encode()
    )
    await writer.drain()
    ack = json.loads((await reader.readuntil(b"\n")).decode())
    assert ack["type"] == "agent.hello_ack"
    assert ack["protocol"] == PROTOCOL_VERSION
    writer.close()
    await writer.wait_closed()


async def test_protocol_mismatch_closes_connection(daemon_and_socket):
    _daemon, path = daemon_and_socket
    reader, writer = await asyncio.open_unix_connection(path)
    writer.write((json.dumps({"type": "agent.hello", "protocol": "blemees/99"}) + "\n").encode())
    await writer.drain()
    err = json.loads((await reader.readuntil(b"\n")).decode())
    assert err["type"] == "agent.error"
    assert err["code"] == "protocol_mismatch"


async def test_open_then_user_then_result(client_factory, fake_mode):
    fake_mode("normal")
    client = await client_factory()
    await client.send(
        {
            "type": "agent.open",
            "id": "r1",
            "session_id": "s1",
            "backend": "claude",
            "options": {"claude": {"tools": ""}},
        }
    )
    opened = await client.wait_for(lambda e: e["type"] == "agent.opened")
    assert opened["session_id"] == "s1"
    await client.send(
        {"type": "agent.user", "session_id": "s1", "message": {"role": "user", "content": "hello"}}
    )
    result = await client.wait_for(
        lambda e: e.get("type") == "agent.result" and e.get("session_id") == "s1"
    )
    assert result["subtype"] == "success"


async def test_codex_backend_open_user_result(client_factory, fake_mode):
    """Drive the daemon end-to-end against fake_codex.py for one turn.

    The agent.* shape should be indistinguishable from the Claude path —
    same envelope, same field names, same usage keys.
    """
    fake_mode("normal")
    client = await client_factory()
    await client.send(
        {
            "type": "agent.open",
            "id": "r1",
            "session_id": "cx-1",
            "backend": "codex",
            "options": {"codex": {"sandbox": "read-only", "approval-policy": "never"}},
        }
    )
    opened = await client.wait_for(lambda e: e["type"] == "agent.opened")
    assert opened["session_id"] == "cx-1"
    assert opened["backend"] == "codex"
    await client.send(
        {
            "type": "agent.user",
            "session_id": "cx-1",
            "message": {"role": "user", "content": "hello"},
        }
    )
    events = await client.wait_for(
        lambda e: e.get("type") == "agent.result" and e.get("session_id") == "cx-1",
        collect=True,
    )
    kinds = [e["type"] for e in events]
    assert "agent.system_init" in kinds
    assert "agent.delta" in kinds
    assert "agent.message" in kinds
    result = events[-1]
    assert result["subtype"] == "success"
    assert result["backend"] == "codex"
    # Usage normalisation: Codex's `cached_input_tokens` is renamed.
    assert "cache_read_input_tokens" in result["usage"]
    assert "reasoning_output_tokens" in result["usage"]
    # The synthesised system_init should carry the codex-specific
    # capabilities envelope.
    init = next(e for e in events if e["type"] == "agent.system_init")
    assert init["model"] == "fake-codex"
    assert init["capabilities"]["sandbox_policy"]["type"] == "read-only"


async def test_unsafe_flag_rejected_on_open(client_factory):
    client = await client_factory()
    await client.send(
        {
            "type": "agent.open",
            "id": "r1",
            "session_id": "s1",
            "backend": "claude",
            "options": {"claude": {"dangerously_skip_permissions": True}},
        }
    )
    err = await client.wait_for(lambda e: e.get("type") == "agent.error")
    assert err["code"] == "unsafe_flag"


async def test_unknown_message_returns_error(client_factory):
    client = await client_factory()
    await client.send({"type": "agent.nonsense", "session_id": "s1"})
    err = await client.wait_for(lambda e: e.get("type") == "agent.error")
    assert err["code"] == "unknown_message"


async def test_ping_replies_with_pong(client_factory):
    client = await client_factory()
    await client.send({"type": "agent.ping", "id": "ping-1", "data": 42})
    pong = await client.wait_for(lambda e: e.get("type") == "agent.pong")
    assert pong["id"] == "ping-1"
    assert pong["data"] == 42


async def test_session_flag_mapping_in_argv(client_factory, fake_mode, argv_trace_path):
    fake_mode("normal")
    client = await client_factory()
    await client.send(
        {
            "type": "agent.open",
            "id": "r1",
            "session_id": "s-map",
            "backend": "claude",
            "options": {
                "claude": {
                    "model": "sonnet",
                    "tools": "",
                    "permission_mode": "bypassPermissions",
                },
            },
        }
    )
    await client.wait_for(lambda e: e.get("type") == "agent.opened")
    # Wait for the first CC event so we know fake_claude has recorded its argv.
    await client.wait_for(lambda e: e.get("type") == "agent.system_init")
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
            "type": "agent.open",
            "id": "r1",
            "session_id": "s-resume",
            "backend": "claude",
            "resume": True,
            "options": {"claude": {"tools": ""}},
        }
    )
    await client.wait_for(lambda e: e.get("type") == "agent.opened")
    await client.wait_for(lambda e: e.get("type") == "agent.system_init")
    argv = json.loads(Path(argv_trace_path).read_text().strip().splitlines()[0])
    assert "--resume" in argv
    assert "--session-id" not in argv


async def test_interrupt_respawns_with_resume(
    client_factory, fake_mode, argv_trace_path, monkeypatch
):
    fake_mode("slow")
    client = await client_factory()
    await client.send(
        {
            "type": "agent.open",
            "id": "r1",
            "session_id": "s-int",
            "backend": "claude",
            "options": {"claude": {"tools": ""}},
        }
    )
    await client.wait_for(lambda e: e.get("type") == "agent.opened")
    await client.send(
        {"type": "agent.user", "session_id": "s-int", "message": {"role": "user", "content": "go"}}
    )
    # Wait until streaming starts.
    await client.wait_for(lambda e: e.get("type") == "agent.delta")
    await client.send({"type": "agent.interrupt", "session_id": "s-int"})
    ir = await client.wait_for(lambda e: e.get("type") == "agent.interrupted")
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


async def test_interrupt_emits_synthesized_agent_result(client_factory, fake_mode):
    """The claude backend synthesises ``agent.result{subtype:"interrupted"}``
    after a kill mid-turn (spec §5.7). The order on the wire must be
    ``agent.interrupted`` first, then the synthesised
    ``agent.result`` — matching how codex emits the equivalent via its
    ``turn_aborted`` event.
    """
    fake_mode("slow")
    client = await client_factory()
    await client.send(
        {
            "type": "agent.open",
            "id": "r1",
            "session_id": "s-synth",
            "backend": "claude",
            "options": {"claude": {"tools": ""}},
        }
    )
    await client.wait_for(lambda e: e.get("type") == "agent.opened")
    await client.send(
        {
            "type": "agent.user",
            "session_id": "s-synth",
            "message": {"role": "user", "content": "go"},
        }
    )
    await client.wait_for(lambda e: e.get("type") == "agent.delta")
    await client.send({"type": "agent.interrupt", "session_id": "s-synth"})

    # Drain frames until both the ack and the synthesised result land.
    seen: dict[str, dict] = {}
    deadline = asyncio.get_running_loop().time() + 5.0
    while not ({"agent.interrupted", "agent.result"} <= seen.keys()):
        remaining = deadline - asyncio.get_running_loop().time()
        assert remaining > 0, f"missing frames; got={list(seen)}"
        evt = await client.recv(timeout=remaining)
        t = evt.get("type")
        if t == "agent.interrupted":
            seen.setdefault("agent.interrupted", evt)
        elif t == "agent.result" and evt.get("session_id") == "s-synth":
            seen.setdefault("agent.result", evt)
    assert seen["agent.interrupted"]["was_idle"] is False
    assert seen["agent.result"]["subtype"] == "interrupted"
    assert seen["agent.result"]["backend"] == "claude"
    # Order check: the synthesised agent.result must arrive *after* the
    # agent.interrupted ack so clients can rely on it as a clean
    # turn-end marker (matching codex's order).
    assert seen["agent.result"]["seq"] > seen["agent.interrupted"].get("seq", 0)


async def test_concurrent_sessions_dont_interfere(client_factory, fake_mode):
    fake_mode("normal")
    client = await client_factory()
    for sid in ("a", "b", "c"):
        await client.send(
            {
                "type": "agent.open",
                "id": f"r{sid}",
                "session_id": sid,
                "backend": "claude",
                "options": {"claude": {"tools": ""}},
            }
        )
    opens = set()
    while len(opens) < 3:
        evt = await client.recv(timeout=5.0)
        if evt.get("type") == "agent.opened":
            opens.add(evt["session_id"])

    for sid in ("a", "b", "c"):
        await client.send(
            {
                "type": "agent.user",
                "session_id": sid,
                "message": {"role": "user", "content": f"hi-{sid}"},
            }
        )

    saw: dict[str, bool] = {}
    while len(saw) < 3:
        evt = await client.recv(timeout=5.0)
        if evt.get("type") == "agent.result" and evt.get("session_id") in {"a", "b", "c"}:
            saw[evt["session_id"]] = True
    assert set(saw) == {"a", "b", "c"}


async def test_crash_mid_turn_then_recover(client_factory, fake_mode, monkeypatch):
    fake_mode("crash")
    client = await client_factory()
    await client.send(
        {
            "type": "agent.open",
            "id": "r1",
            "session_id": "s-crash",
            "backend": "claude",
            "options": {"claude": {"tools": ""}},
        }
    )
    await client.wait_for(lambda e: e.get("type") == "agent.opened")
    await client.send(
        {
            "type": "agent.user",
            "session_id": "s-crash",
            "message": {"role": "user", "content": "boom"},
        }
    )
    err = await client.wait_for(
        lambda e: e.get("type") == "agent.error" and e.get("code") == "backend_crashed"
    )
    assert err["session_id"] == "s-crash"

    # Next user message should transparently respawn (spec §9.1).
    fake_mode("normal")
    await client.send(
        {
            "type": "agent.user",
            "session_id": "s-crash",
            "message": {"role": "user", "content": "again"},
        }
    )
    await client.wait_for(
        lambda e: e.get("type") == "agent.result" and e.get("session_id") == "s-crash"
    )


async def test_close_removes_session(client_factory, fake_mode):
    fake_mode("normal")
    client = await client_factory()
    await client.send(
        {
            "type": "agent.open",
            "id": "r1",
            "session_id": "s-close",
            "backend": "claude",
            "options": {"claude": {"tools": ""}},
        }
    )
    await client.wait_for(lambda e: e.get("type") == "agent.opened")
    await client.send({"type": "agent.close", "id": "r2", "session_id": "s-close", "delete": False})
    closed = await client.wait_for(lambda e: e.get("type") == "agent.closed")
    assert closed["session_id"] == "s-close"

    # Re-open without resume should succeed since session is gone.
    await client.send(
        {
            "type": "agent.open",
            "id": "r3",
            "session_id": "s-close",
            "backend": "claude",
            "options": {"claude": {"tools": ""}},
        }
    )
    await client.wait_for(lambda e: e.get("type") == "agent.opened")


async def test_detach_allows_reattach_via_resume(client_factory, fake_mode):
    fake_mode("normal")
    c1 = await client_factory()
    await c1.send(
        {
            "type": "agent.open",
            "id": "r1",
            "session_id": "s-det",
            "backend": "claude",
            "options": {"claude": {"tools": ""}},
        }
    )
    await c1.wait_for(lambda e: e.get("type") == "agent.opened")
    await c1.close()

    # New connection can reattach via resume:true.
    c2 = await client_factory()
    await c2.send(
        {
            "type": "agent.open",
            "id": "r2",
            "session_id": "s-det",
            "backend": "claude",
            "resume": True,
            "options": {"claude": {"tools": ""}},
        }
    )
    await c2.wait_for(lambda e: e.get("type") == "agent.opened")


async def test_invalid_message_keeps_connection_alive(client_factory):
    client = await client_factory()
    # Send a malformed open (no session).
    await client.send({"type": "agent.open", "id": "bad"})
    err = await client.wait_for(lambda e: e.get("type") == "agent.error")
    assert err["code"] == "invalid_message"
    # Connection still usable: a truly unknown type still returns unknown.
    await client.send({"type": "agent.nonsense_xyz"})
    err2 = await client.wait_for(lambda e: e.get("type") == "agent.error")
    assert err2["code"] == "unknown_message"


# ---------------------------------------------------------------------------
# list_sessions
# ---------------------------------------------------------------------------


def _seed_transcript(
    home_dir: Path, cwd: str, session_id: str, preview: str | None, mtime: int
) -> Path:
    from blemees_agent.backends.claude import project_dir_for_cwd as _pdfc

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
    # Real CC transcripts carry `cwd` at the top level on most records;
    # mirror that so the daemon's metadata-scan finds it during all-cwds
    # listings.
    lines = [json.dumps({"type": "system", "subtype": "init", "cwd": cwd})]
    if preview is not None:
        lines.append(
            json.dumps(
                {
                    "type": "user",
                    "cwd": cwd,
                    "message": {"role": "user", "content": preview},
                }
            )
        )
    path = project_dir / f"{session_id}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


async def test_list_sessions_empty_body_unions_disk_and_live(
    client_factory, fake_mode, tmp_path, monkeypatch
):
    """No filters → walk every project on disk AND every live session.
    The union is returned; reply omits top-level `cwd`."""
    monkeypatch.setenv("HOME", str(tmp_path))
    cwd_a = str(tmp_path / "a")
    cwd_b = str(tmp_path / "b")
    Path(cwd_a).mkdir(parents=True)
    Path(cwd_b).mkdir(parents=True)
    _seed_transcript(tmp_path, cwd_a, "cold-a", "old A", 1_700_000_000)
    _seed_transcript(tmp_path, cwd_b, "cold-b", "old B", 1_700_000_100)

    fake_mode("normal")
    client = await client_factory()
    await client.send(
        {
            "type": "agent.open",
            "id": "open-1",
            "session_id": "warm",
            "backend": "claude",
            "options": {"claude": {"tools": "", "cwd": cwd_a}},
        }
    )
    await client.wait_for(lambda e: e.get("type") == "agent.opened")

    await client.send({"type": "agent.list_sessions", "id": "r1"})
    reply = await client.wait_for(lambda e: e.get("type") == "agent.sessions")
    assert reply["id"] == "r1"
    assert "cwd" not in reply  # no filter echoed
    ids = {s["session_id"] for s in reply["sessions"]}
    assert {"warm", "cold-a", "cold-b"}.issubset(ids)

    # All-cwds disk rows carry their own cwd so clients can group.
    rows = {s["session_id"]: s for s in reply["sessions"]}
    assert rows["cold-a"]["cwd"] == cwd_a
    assert rows["cold-b"]["cwd"] == cwd_b
    # The live row keeps its rich fields.
    assert rows["warm"]["attached"] is True
    assert rows["warm"]["cwd"] == cwd_a


async def test_list_sessions_live_true_returns_all_live(client_factory, fake_mode, tmp_path):
    """`live:true` skips the disk scan; live overlay across all cwds."""
    fake_mode("normal")
    cwd_a = tmp_path / "a"
    cwd_b = tmp_path / "b"
    cwd_a.mkdir()
    cwd_b.mkdir()
    client = await client_factory()
    for sid, cwd in (("liveA", str(cwd_a)), ("liveB", str(cwd_b))):
        await client.send(
            {
                "type": "agent.open",
                "id": f"open-{sid}",
                "session_id": sid,
                "backend": "claude",
                "options": {"claude": {"tools": "", "cwd": cwd}},
            }
        )
        await client.wait_for(
            lambda e, s=sid: e.get("type") == "agent.opened" and e.get("session_id") == s
        )

    await client.send({"type": "agent.list_sessions", "id": "r1", "live": True})
    reply = await client.wait_for(lambda e: e.get("type") == "agent.sessions")
    ids = {s["session_id"] for s in reply["sessions"]}
    assert {"liveA", "liveB"}.issubset(ids)
    assert "cwd" not in reply
    rows = {s["session_id"]: s for s in reply["sessions"]}
    assert rows["liveA"]["attached"] is True
    assert rows["liveA"]["backend"] == "claude"
    assert rows["liveA"]["cwd"] == str(cwd_a)
    assert "started_at_ms" in rows["liveA"]
    assert "last_seq" in rows["liveA"]
    assert rows["liveA"]["turn_active"] is False


async def test_list_sessions_live_false_excludes_currently_live(
    client_factory, fake_mode, tmp_path, monkeypatch
):
    """`live:false` returns cold sessions only. A session with both an
    in-memory record and a disk transcript counts as live, not cold,
    and is excluded from the result."""
    monkeypatch.setenv("HOME", str(tmp_path))
    work = tmp_path / "work"
    work.mkdir()
    cwd = str(work)
    _seed_transcript(tmp_path, cwd, "old-cold", "long ago", 1_700_000_000)
    # `warm` exists both as an open live session and as an on-disk
    # transcript (we seed it before opening, then open with the same
    # session_id so the daemon's in-memory record overlays the disk).
    _seed_transcript(tmp_path, cwd, "warm", "in flight", 1_700_000_100)

    fake_mode("normal")
    client = await client_factory()
    await client.send(
        {
            "type": "agent.open",
            "id": "open-1",
            "session_id": "warm",
            "backend": "claude",
            "options": {"claude": {"tools": "", "cwd": cwd}},
            "resume": True,
        }
    )
    await client.wait_for(lambda e: e.get("type") == "agent.opened")

    await client.send({"type": "agent.list_sessions", "id": "r1", "cwd": cwd, "live": False})
    reply = await client.wait_for(lambda e: e.get("type") == "agent.sessions")
    ids = {s["session_id"] for s in reply["sessions"]}
    assert "old-cold" in ids
    assert "warm" not in ids  # live, so excluded from the cold-only set


async def test_list_sessions_cwd_plus_live_true_skips_disk(
    client_factory, fake_mode, tmp_path, monkeypatch
):
    """`cwd:X, live:true` filters to that cwd's live sessions and
    deliberately ignores on-disk transcripts."""
    monkeypatch.setenv("HOME", str(tmp_path))
    work = tmp_path / "work"
    work.mkdir()
    cwd = str(work)
    _seed_transcript(tmp_path, cwd, "old-cold", "stale", 1_700_000_000)

    fake_mode("normal")
    client = await client_factory()
    await client.send(
        {
            "type": "agent.open",
            "id": "open-1",
            "session_id": "warm",
            "backend": "claude",
            "options": {"claude": {"tools": "", "cwd": cwd}},
        }
    )
    await client.wait_for(lambda e: e.get("type") == "agent.opened")

    await client.send({"type": "agent.list_sessions", "id": "r1", "cwd": cwd, "live": True})
    reply = await client.wait_for(lambda e: e.get("type") == "agent.sessions")
    ids = {s["session_id"] for s in reply["sessions"]}
    assert "warm" in ids
    assert "old-cold" not in ids  # disk scan was skipped
    assert reply["cwd"] == cwd


async def test_list_sessions_empty_for_unknown_cwd(client_factory, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    client = await client_factory()
    await client.send({"type": "agent.list_sessions", "id": "r1", "cwd": "/nope/nope"})
    reply = await client.wait_for(lambda e: e.get("type") == "agent.sessions")
    assert reply["id"] == "r1"
    assert reply["cwd"] == "/nope/nope"
    assert reply["sessions"] == []


async def test_list_sessions_returns_sorted_on_disk(client_factory, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cwd = "/home/u/proj"
    _seed_transcript(tmp_path, cwd, "older", "old one", 1_700_000_000)
    _seed_transcript(tmp_path, cwd, "newer", "new one", 1_700_000_100)

    client = await client_factory()
    await client.send({"type": "agent.list_sessions", "id": "r1", "cwd": cwd})
    reply = await client.wait_for(lambda e: e.get("type") == "agent.sessions")
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
            "type": "agent.open",
            "id": "r1",
            "session_id": "live",
            "backend": "claude",
            "options": {"claude": {"tools": "", "cwd": cwd}},
        }
    )
    await client.wait_for(lambda e: e.get("type") == "agent.opened")

    await client.send({"type": "agent.list_sessions", "id": "r2", "cwd": cwd})
    reply = await client.wait_for(lambda e: e.get("type") == "agent.sessions")
    records = {s["session_id"]: s for s in reply["sessions"]}
    assert "live" in records
    assert records["live"]["attached"] is True


def _seed_codex_rollout(
    home_dir: Path,
    *,
    cwd: str,
    thread_id: str,
    user_text: str | None = None,
    mtime: int | None = None,
) -> Path:
    """Mirror the helper in test_backend_codex but inside test_daemon_mock
    so the daemon's list_sessions sees a Codex rollout under $HOME."""
    day_dir = home_dir / ".codex" / "sessions" / "2026" / "04" / "27"
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / f"rollout-2026-04-27T14-42-22-{thread_id}.jsonl"
    lines = [
        json.dumps(
            {
                "type": "session_configured",
                "session_id": thread_id,
                "model": "gpt-5.4",
                "cwd": cwd,
                "rollout_path": str(path),
            }
        )
    ]
    if user_text is not None:
        lines.append(
            json.dumps(
                {
                    "type": "item_completed",
                    "item": {
                        "type": "UserMessage",
                        "id": "u",
                        "content": [{"type": "text", "text": user_text}],
                    },
                }
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


async def test_list_sessions_merges_claude_and_codex_rows(client_factory, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cwd = "/work/repo"
    _seed_transcript(tmp_path, cwd, "claude-1", "claude prompt", 1_700_000_000)
    _seed_codex_rollout(
        tmp_path,
        cwd=cwd,
        thread_id="019dd03f-aaaa-0000-0000-000000000000",
        user_text="codex prompt",
        mtime=1_700_000_500,
    )

    client = await client_factory()
    await client.send({"type": "agent.list_sessions", "id": "r1", "cwd": cwd})
    reply = await client.wait_for(lambda e: e.get("type") == "agent.sessions")
    rows = reply["sessions"]
    backends = {r["session_id"]: r["backend"] for r in rows}
    assert backends["claude-1"] == "claude"
    assert backends["019dd03f-aaaa-0000-0000-000000000000"] == "codex"
    # Codex row is newer → appears first.
    assert rows[0]["backend"] == "codex"
    assert rows[0]["preview"] == "codex prompt"


async def test_codex_session_resume_uses_codex_reply(
    client_factory, fake_mode, tmp_path, monkeypatch
):
    """Reattach via resume:true should pass the cached threadId to the
    new CodexBackend so the next turn routes via `codex-reply`.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    fake_mode("normal")
    rpc_log = tmp_path / "rpc.jsonl"
    monkeypatch.setenv("BLEMEES_FAKE_RPC_LOG", str(rpc_log))

    c1 = await client_factory()
    await c1.send(
        {
            "type": "agent.open",
            "id": "r1",
            "session_id": "cx-resume",
            "backend": "codex",
            "options": {"codex": {"sandbox": "read-only"}},
        }
    )
    await c1.wait_for(lambda e: e.get("type") == "agent.opened")
    await c1.send(
        {
            "type": "agent.user",
            "session_id": "cx-resume",
            "message": {"role": "user", "content": "hi"},
        }
    )
    c1_events = await c1.wait_for(
        lambda e: e.get("type") == "agent.result" and e.get("session_id") == "cx-resume",
        collect=True,
    )
    last_seq = c1_events[-1]["seq"]
    await c1.close()
    # Let the daemon's connection-detach finish (it runs in serve()'s
    # finally block; c2.open racing it would find sess.backend still
    # alive and skip the respawn — giving us no `codex-reply` call).
    await asyncio.sleep(0.1)

    # Reattach. Pass last_seen_seq so c2 doesn't get a replay of c1's
    # already-consumed frames — the daemon should respawn with the
    # cached threadId so the new backend's first call is `codex-reply`.
    c2 = await client_factory()
    await c2.send(
        {
            "type": "agent.open",
            "id": "r2",
            "session_id": "cx-resume",
            "backend": "codex",
            "resume": True,
            "last_seen_seq": last_seq,
            "options": {"codex": {"sandbox": "read-only"}},
        }
    )
    opened = await c2.wait_for(lambda e: e.get("type") == "agent.opened")
    # native_session_id should be populated now (we know the threadId
    # from the previous turn).
    assert opened.get("native_session_id"), opened

    await c2.send(
        {
            "type": "agent.user",
            "session_id": "cx-resume",
            "message": {"role": "user", "content": "again"},
        }
    )
    await c2.wait_for(
        lambda e: e.get("type") == "agent.result" and e.get("session_id") == "cx-resume"
    )

    calls = [
        json.loads(ln) for ln in rpc_log.read_text(encoding="utf-8").splitlines() if ln.strip()
    ]
    tools = [c["tool"] for c in calls]
    assert tools == ["codex", "codex-reply"], tools
    assert calls[1]["thread_id"]


async def test_status_by_backend_counts_mixed_sessions(client_factory, fake_mode):
    fake_mode("normal")
    client = await client_factory()

    await client.send(
        {
            "type": "agent.open",
            "id": "rc",
            "session_id": "claude-1",
            "backend": "claude",
            "options": {"claude": {"tools": ""}},
        }
    )
    await client.wait_for(
        lambda e: e.get("type") == "agent.opened" and e.get("session_id") == "claude-1"
    )

    await client.send(
        {
            "type": "agent.open",
            "id": "rx",
            "session_id": "codex-1",
            "backend": "codex",
            "options": {"codex": {}},
        }
    )
    await client.wait_for(
        lambda e: e.get("type") == "agent.opened" and e.get("session_id") == "codex-1"
    )

    await client.send({"type": "agent.status", "id": "s1"})
    snap = await client.wait_for(lambda e: e.get("type") == "agent.status_reply")
    by_backend = snap["sessions"]["by_backend"]
    assert by_backend.get("claude") == 1, snap
    assert by_backend.get("codex") == 1, snap
    assert snap["sessions"]["total"] == 2


async def test_codex_close_with_delete_preserves_rollout(
    client_factory, fake_mode, tmp_path, monkeypatch
):
    """`close{delete:true}` removes daemon-owned state (event log,
    usage sidecar) but leaves codex's rollout under
    ``~/.codex/sessions/`` alone — codex manages its own directory and
    tracks rollouts in an internal state DB. Deleting the file behind
    its back surfaced as ``state db returned stale rollout path …``
    ERROR spam on subsequent codex startups.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    fake_mode("normal")

    client = await client_factory()
    await client.send(
        {
            "type": "agent.open",
            "id": "r1",
            "session_id": "cx-del",
            "backend": "codex",
            "options": {"codex": {}},
        }
    )
    await client.wait_for(lambda e: e.get("type") == "agent.opened")
    await client.send(
        {
            "type": "agent.user",
            "session_id": "cx-del",
            "message": {"role": "user", "content": "hi"},
        }
    )
    # Wait for system_init so the rollout_path is captured.
    await client.wait_for(
        lambda e: e.get("type") == "agent.system_init" and e.get("session_id") == "cx-del"
    )
    await client.wait_for(
        lambda e: e.get("type") == "agent.result" and e.get("session_id") == "cx-del"
    )

    # The fake codex rollout path is /tmp/fake-rollout.jsonl — write a
    # stand-in to verify the daemon does *not* unlink it.
    fake_rollout = Path("/tmp/fake-rollout.jsonl")
    fake_rollout.write_text("{}\n", encoding="utf-8")
    try:
        await client.send(
            {
                "type": "agent.close",
                "id": "r2",
                "session_id": "cx-del",
                "delete": True,
            }
        )
        await client.wait_for(lambda e: e.get("type") == "agent.closed")
        assert fake_rollout.is_file(), (
            "backend rollout should not be deleted (codex's directory is its own)"
        )
    finally:
        if fake_rollout.is_file():
            fake_rollout.unlink()


# ---------------------------------------------------------------------------
# Session takeover
# ---------------------------------------------------------------------------


async def test_takeover_notifies_previous_owner(client_factory, fake_mode):
    fake_mode("normal")
    a = await client_factory()
    b = await client_factory()

    await a.send(
        {
            "type": "agent.open",
            "id": "r1",
            "session_id": "shared",
            "backend": "claude",
            "options": {"claude": {"tools": ""}},
        }
    )
    await a.wait_for(lambda e: e.get("type") == "agent.opened")

    # B takes over via resume=true.
    await b.send(
        {
            "type": "agent.open",
            "id": "r2",
            "session_id": "shared",
            "backend": "claude",
            "resume": True,
            "options": {"claude": {"tools": ""}},
        }
    )

    # A must see the notification.
    notice = await a.wait_for(
        lambda e: e.get("type") == "agent.session_taken" and e.get("session_id") == "shared"
    )
    # Informational peer_pid may be absent in tests (no SO_PEERCRED capture
    # for in-process unix sockets on some kernels), but the frame must arrive.
    assert notice["session_id"] == "shared"

    # B's ack arrives and the event stream now flows to B.
    await b.wait_for(lambda e: e.get("type") == "agent.opened")
    await b.send(
        {
            "type": "agent.user",
            "session_id": "shared",
            "message": {"role": "user", "content": "hi"},
        }
    )
    await b.wait_for(lambda e: e.get("type") == "agent.result" and e.get("session_id") == "shared")


async def test_no_takeover_notice_for_same_connection_reopen(client_factory, fake_mode):
    fake_mode("normal")
    c = await client_factory()
    await c.send(
        {
            "type": "agent.open",
            "id": "r1",
            "session_id": "self",
            "backend": "claude",
            "options": {"claude": {"tools": ""}},
        }
    )
    await c.wait_for(lambda e: e.get("type") == "agent.opened")

    # Reopen from the same connection with resume=true — no takeover.
    await c.send(
        {
            "type": "agent.open",
            "id": "r2",
            "session_id": "self",
            "backend": "claude",
            "resume": True,
            "options": {"claude": {"tools": ""}},
        }
    )
    # The opened ack arrives; no session_taken in between.
    collected = await c.wait_for(
        lambda e: e.get("id") == "r2" and e.get("type") == "agent.opened",
        collect=True,
    )
    assert not any(e.get("type") == "agent.session_taken" for e in collected)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


async def test_status_returns_snapshot(client_factory, fake_mode):
    fake_mode("normal")
    client = await client_factory()
    await client.send({"type": "agent.status", "id": "s-1"})
    reply = await client.wait_for(lambda e: e.get("type") == "agent.status_reply")
    assert reply["id"] == "s-1"
    assert reply["protocol"] == "blemees-agent/1"
    assert reply["daemon"].startswith("blemees-agentd/")
    assert reply["pid"] > 0
    assert reply["uptime_s"] >= 0.0
    assert reply["connections"] >= 1
    assert "backends" in reply  # may be empty if no upstream binaries detected
    assert reply["sessions"]["total"] == 0
    assert reply["sessions"]["attached"] == 0
    assert reply["sessions"]["detached"] == 0
    assert reply["sessions"]["active_turns"] == 0
    assert reply["sessions"]["by_backend"] == {}
    cfg = reply["config"]
    assert cfg["ring_buffer_size"] > 0
    assert "shutdown_grace_s" in cfg


async def test_status_reflects_open_sessions(client_factory, fake_mode):
    fake_mode("normal")
    client = await client_factory()
    await client.send(
        {
            "type": "agent.open",
            "id": "r1",
            "session_id": "x",
            "backend": "claude",
            "options": {"claude": {"tools": ""}},
        }
    )
    await client.wait_for(lambda e: e.get("type") == "agent.opened")
    await client.send({"type": "agent.status"})
    reply = await client.wait_for(lambda e: e.get("type") == "agent.status_reply")
    assert reply["sessions"]["total"] == 1
    assert reply["sessions"]["attached"] == 1


# ---------------------------------------------------------------------------
# watch / unwatch
# ---------------------------------------------------------------------------


async def test_watch_receives_events_live(client_factory, fake_mode):
    fake_mode("normal")
    owner = await client_factory()
    watcher = await client_factory()

    await owner.send(
        {
            "type": "agent.open",
            "id": "r1",
            "session_id": "shared",
            "backend": "claude",
            "options": {"claude": {"tools": ""}},
        }
    )
    await owner.wait_for(lambda e: e.get("type") == "agent.opened")

    await watcher.send({"type": "agent.watch", "id": "w1", "session_id": "shared"})
    ack = await watcher.wait_for(lambda e: e.get("type") == "agent.watching")
    assert ack["session_id"] == "shared"

    # Drive a turn on the owner; the watcher should see the same event stream.
    await owner.send(
        {
            "type": "agent.user",
            "session_id": "shared",
            "message": {"role": "user", "content": "hi"},
        }
    )
    await watcher.wait_for(
        lambda e: e.get("type") == "agent.result" and e.get("session_id") == "shared"
    )


async def test_watch_replays_from_last_seen_seq(client_factory, fake_mode):
    fake_mode("normal")
    owner = await client_factory()
    await owner.send(
        {
            "type": "agent.open",
            "id": "r1",
            "session_id": "rep",
            "backend": "claude",
            "options": {"claude": {"tools": ""}},
        }
    )
    await owner.wait_for(lambda e: e.get("type") == "agent.opened")
    await owner.send(
        {
            "type": "agent.user",
            "session_id": "rep",
            "message": {"role": "user", "content": "hi"},
        }
    )
    await owner.wait_for(lambda e: e.get("type") == "agent.result" and e.get("session_id") == "rep")

    watcher = await client_factory()
    await watcher.send(
        {
            "type": "agent.watch",
            "id": "w1",
            "session_id": "rep",
            "last_seen_seq": 0,
        }
    )
    await watcher.wait_for(lambda e: e.get("type") == "agent.watching")
    # Should catch up through the replay and see the completed turn.
    await watcher.wait_for(
        lambda e: e.get("type") == "agent.result" and e.get("session_id") == "rep"
    )


async def test_unwatch_stops_delivery(client_factory, fake_mode):
    fake_mode("normal")
    owner = await client_factory()
    watcher = await client_factory()
    await owner.send(
        {
            "type": "agent.open",
            "id": "r1",
            "session_id": "u",
            "backend": "claude",
            "options": {"claude": {"tools": ""}},
        }
    )
    await owner.wait_for(lambda e: e.get("type") == "agent.opened")
    await watcher.send({"type": "agent.watch", "id": "w1", "session_id": "u"})
    await watcher.wait_for(lambda e: e.get("type") == "agent.watching")

    await watcher.send({"type": "agent.unwatch", "id": "u1", "session_id": "u"})
    ack = await watcher.wait_for(lambda e: e.get("type") == "agent.unwatched")
    assert ack["was_watching"] is True

    # Now drive a turn; the watcher should NOT see agent.result.
    await owner.send(
        {
            "type": "agent.user",
            "session_id": "u",
            "message": {"role": "user", "content": "go"},
        }
    )
    await owner.wait_for(lambda e: e.get("type") == "agent.result" and e.get("session_id") == "u")
    # Give event propagation a beat, then confirm the watcher queue is idle.
    await asyncio.sleep(0.1)
    try:
        evt = await watcher.recv(timeout=0.2)
        assert evt.get("type") != "agent.result", f"unexpected frame {evt}"
    except TimeoutError:
        pass


async def test_watch_unknown_session_errors(client_factory):
    client = await client_factory()
    await client.send({"type": "agent.watch", "id": "w1", "session_id": "ghost"})
    err = await client.wait_for(lambda e: e.get("type") == "agent.error")
    assert err["code"] == "session_unknown"


async def test_watcher_receives_session_closed_when_owner_closes(client_factory, fake_mode):
    """When the owner sends `agent.close`, every watcher gets a
    `agent.session_closed{reason:"owner_closed"}` notification before
    the session is removed."""
    fake_mode("normal")
    owner = await client_factory()
    watcher = await client_factory()
    await owner.send(
        {
            "type": "agent.open",
            "id": "r1",
            "session_id": "closer",
            "backend": "claude",
            "options": {"claude": {"tools": ""}},
        }
    )
    await owner.wait_for(lambda e: e.get("type") == "agent.opened")
    await watcher.send({"type": "agent.watch", "id": "w1", "session_id": "closer"})
    await watcher.wait_for(lambda e: e.get("type") == "agent.watching")

    await owner.send({"type": "agent.close", "id": "c1", "session_id": "closer", "delete": False})
    closed_ack = await owner.wait_for(lambda e: e.get("type") == "agent.closed")
    assert closed_ack["session_id"] == "closer"

    notice = await watcher.wait_for(lambda e: e.get("type") == "agent.session_closed")
    assert notice["session_id"] == "closer"
    assert notice["reason"] == "owner_closed"


async def test_session_closed_not_sent_to_owner(client_factory, fake_mode):
    """The closer does NOT also receive `session_closed` — they get the
    `closed` ack instead. Watchers and owners get distinct signals."""
    fake_mode("normal")
    client = await client_factory()
    await client.send(
        {
            "type": "agent.open",
            "id": "r1",
            "session_id": "solo",
            "backend": "claude",
            "options": {"claude": {"tools": ""}},
        }
    )
    await client.wait_for(lambda e: e.get("type") == "agent.opened")
    await client.send({"type": "agent.close", "id": "c1", "session_id": "solo", "delete": False})
    await client.wait_for(lambda e: e.get("type") == "agent.closed")

    # Drain anything else; assert no session_closed leaked back to the owner.
    await asyncio.sleep(0.1)
    try:
        while True:
            evt = await client.recv(timeout=0.1)
            assert evt.get("type") != "agent.session_closed", evt
    except TimeoutError:
        pass


# ---------------------------------------------------------------------------
# session_info
# ---------------------------------------------------------------------------


async def test_session_info_unknown_session_errors(client_factory):
    client = await client_factory()
    await client.send({"type": "agent.session_info", "id": "i1", "session_id": "nope"})
    err = await client.wait_for(lambda e: e.get("type") == "agent.error")
    assert err["code"] == "session_unknown"


async def test_session_info_finds_on_disk_session(client_factory, monkeypatch, tmp_path):
    """A session that's on-disk but not in memory still returns session_info.

    Plant a synthetic CC transcript under a fake $HOME and ask for its
    info. The reply carries ``backend`` / ``cwd`` / ``model`` from the
    transcript head; usage counters are zeros (no durable sidecar).
    """
    fake_home = tmp_path / "fake-home"
    proj_cwd = "/tmp/repro-on-disk-cwd"
    encoded = proj_cwd.replace("/", "-").lstrip("-")
    proj_dir = fake_home / ".claude" / "projects" / f"-{encoded}"
    proj_dir.mkdir(parents=True)
    sid = "9b9b9b9b-1234-1234-1234-9b9b9b9b9b9b"
    (proj_dir / f"{sid}.jsonl").write_text(
        json.dumps(
            {
                "type": "user",
                "cwd": proj_cwd,
                "model": "claude-sonnet-4-6",
                "sessionId": sid,
                "message": {"role": "user", "content": "hi"},
            }
        )
        + "\n"
    )
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    client = await client_factory()
    await client.send({"type": "agent.session_info", "id": "i1", "session_id": sid})
    reply = await client.wait_for(lambda e: e.get("type") == "agent.session_info_reply")
    assert reply["backend"] == "claude"
    assert reply["session_id"] == sid
    assert reply["native_session_id"] == sid
    assert reply["cwd"] == proj_cwd
    assert reply["model"] == "claude-sonnet-4-6"
    assert reply["turns"] == 0
    assert reply["attached"] is False
    assert reply["subprocess_running"] is False
    assert reply["context_tokens"] == 0
    assert reply["last_turn_usage"] == {}
    assert reply["cumulative_usage"] == {}


async def test_session_info_accumulates_across_turns(client_factory, fake_mode):
    fake_mode("normal")
    c = await client_factory()
    await c.send(
        {
            "type": "agent.open",
            "id": "r1",
            "session_id": "u",
            "backend": "claude",
            "options": {"claude": {"tools": ""}},
        }
    )
    await c.wait_for(lambda e: e.get("type") == "agent.opened")

    # Zero counters before any turn.
    await c.send({"type": "agent.session_info", "id": "i0", "session_id": "u"})
    zero = await c.wait_for(lambda e: e.get("type") == "agent.session_info_reply")
    assert zero["turns"] == 0
    assert zero["cumulative_usage"]["input_tokens"] == 0

    # One turn (fake emits usage: in=10, out=5).
    for i in range(3):
        await c.send(
            {
                "type": "agent.user",
                "session_id": "u",
                "message": {"role": "user", "content": f"hi {i}"},
            }
        )
        await c.wait_for(lambda e: e.get("type") == "agent.result" and e.get("session_id") == "u")

    await c.send({"type": "agent.session_info", "id": "i1", "session_id": "u"})
    info = await c.wait_for(lambda e: e.get("type") == "agent.session_info_reply")
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
    from blemees_agent import PROTOCOL_VERSION
    from blemees_agent.config import Config
    from blemees_agent.daemon import Daemon
    from blemees_agent.logging import configure

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
                "type": "agent.hello",
                "client": "t/0",
                "protocol": PROTOCOL_VERSION,
            }
        )
        await recv()  # hello_ack
        return w, send, wait_for

    from tests.blemees_agent.conftest import short_socket_path

    # ----- first daemon: run two turns, confirm counters, shut down.
    sock1 = short_socket_path("blemeesd-persist1")
    cfg1 = _cfg(sock1)
    d1 = Daemon(cfg1, configure("error"))
    await d1.start()
    t1 = asyncio.create_task(d1.serve_forever())
    try:
        w, send, wait_for = await _connect(sock1)
        await send(
            {
                "type": "agent.open",
                "id": "r1",
                "session_id": "keep",
                "backend": "claude",
                "options": {"claude": {"tools": ""}},
            }
        )
        await wait_for(lambda e: e.get("type") == "agent.opened")
        for _ in range(2):
            await send(
                {
                    "type": "agent.user",
                    "session_id": "keep",
                    "message": {"role": "user", "content": "hi"},
                }
            )
            await wait_for(
                lambda e: e.get("type") == "agent.result" and e.get("session_id") == "keep"
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
                "type": "agent.open",
                "id": "r2",
                "session_id": "keep",
                "backend": "claude",
                "resume": True,
                "options": {"claude": {"tools": ""}},
            }
        )
        await wait_for(lambda e: e.get("type") == "agent.opened")
        await send({"type": "agent.session_info", "id": "i1", "session_id": "keep"})
        info = await wait_for(lambda e: e.get("type") == "agent.session_info_reply")
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
