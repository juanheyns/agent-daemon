"""Unit tests for the ClaudeBackend wrapper using the fake claude stub."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from blemees.backends.claude import (
    ClaudeBackend,
    argv_to_resume,
    build_argv,
    build_user_stdin_line,
    list_on_disk_sessions,
    project_dir_for_cwd,
)
from blemees.backends.translate_claude import translate_event
from blemees.errors import SessionBusyError
from blemees.logging import configure

FAKE_CLAUDE = str(Path(__file__).parent / "fake_claude.py")


def _make_argv(session: str, *, for_resume: bool = False) -> list[str]:
    return build_argv(
        FAKE_CLAUDE,
        session_id=session,
        options={"tools": ""},
        for_resume=for_resume,
    )


# ---------------------------------------------------------------------------
# translate_claude — pure-function tests
# ---------------------------------------------------------------------------


def test_rate_limit_event_maps_to_unified_rate_limits_notice():
    """CC's `rate_limit_event` lands as `agent.notice{rate_limits}` —
    same `category` codex emits for its `token_count` rate-limit ping,
    so a backend-agnostic client can filter by category.
    """
    cc_event = {
        "type": "rate_limit_event",
        "tokens_left": 12345,
        "reset_at": 1900000000,
        "model": "claude-sonnet-4-6",
    }
    out = translate_event(cc_event)
    assert len(out) == 1
    notice = out[0]
    assert notice["type"] == "agent.notice"
    assert notice["level"] == "info"
    assert notice["category"] == "rate_limits"
    # All non-`type` fields propagate under data, so future CC additions
    # land without code changes.
    assert notice["data"] == {
        "tokens_left": 12345,
        "reset_at": 1900000000,
        "model": "claude-sonnet-4-6",
    }


def test_rate_limit_event_with_no_extra_fields_omits_data():
    out = translate_event({"type": "rate_limit_event"})
    assert out == [{"type": "agent.notice", "level": "info", "category": "rate_limits"}]


def test_rate_limit_event_carries_raw_when_requested():
    out = translate_event({"type": "rate_limit_event", "x": 1}, include_raw=True)
    assert out[0]["raw"] == {"type": "rate_limit_event", "x": 1}


# ---------------------------------------------------------------------------
# argv_to_resume
# ---------------------------------------------------------------------------


def test_argv_to_resume_rewrites():
    argv = ["claude", "-p", "--session-id", "abc", "--input-format", "x"]
    out = argv_to_resume(argv, "abc")
    assert "--resume" in out and "--session-id" not in out
    assert out[out.index("--resume") + 1] == "abc"


def test_argv_to_resume_is_idempotent_when_already_resume():
    argv = ["claude", "-p", "--resume", "abc"]
    out = argv_to_resume(argv, "abc")
    assert out.count("--resume") == 1


# ---------------------------------------------------------------------------
# Live subprocess interactions (against the fake claude stub).
# ---------------------------------------------------------------------------


async def _drain_until_result(queue: asyncio.Queue, session: str) -> list[dict]:
    events = []
    while True:
        evt = await asyncio.wait_for(queue.get(), timeout=5.0)
        events.append(evt)
        if evt.get("type") == "agent.result":
            return events


async def test_normal_turn_produces_result(monkeypatch):
    monkeypatch.setenv("BLEMEES_FAKE_MODE", "normal")
    queue: asyncio.Queue = asyncio.Queue()
    logger = configure("error")
    proc = ClaudeBackend(
        session_id="s1",
        argv=_make_argv("s1"),
        cwd=None,
        on_event=queue.put,
        logger=logger,
    )
    await proc.spawn()
    try:
        assert proc.running is True
        await proc.send_user_turn({"role": "user", "content": "hi"})
        events = await _drain_until_result(queue, "s1")
        kinds = [e["type"] for e in events]
        assert "agent.system_init" in kinds
        # Translator drops content_block_start for text; we expect deltas + assistant.
        assert "agent.delta" in kinds
        assert "agent.message" in kinds
        assert events[-1]["type"] == "agent.result"
        assert events[-1]["backend"] == "claude"
        # turn_id and time_to_first_token_ms are daemon-synthesised so the
        # claude backend's terminal frame matches codex's shape.
        assert isinstance(events[-1].get("turn_id"), str)
        assert isinstance(events[-1].get("time_to_first_token_ms"), int)
        assert events[-1]["time_to_first_token_ms"] >= 0
        assert proc.turn_active is False
    finally:
        await proc.close()


async def test_send_user_turn_emits_task_started_notice(monkeypatch):
    """The daemon synthesises `agent.notice{category:"task_started"}` so
    Claude has the same turn-start hook codex emits natively."""
    monkeypatch.setenv("BLEMEES_FAKE_MODE", "normal")
    queue: asyncio.Queue = asyncio.Queue()
    logger = configure("error")
    proc = ClaudeBackend(
        session_id="s1",
        argv=_make_argv("s1"),
        cwd=None,
        on_event=queue.put,
        logger=logger,
    )
    await proc.spawn()
    try:
        await proc.send_user_turn({"role": "user", "content": "hi"})
        events = await _drain_until_result(queue, "s1")
        notices = [e for e in events if e.get("type") == "agent.notice"]
        task_started = [n for n in notices if n.get("category") == "task_started"]
        assert len(task_started) == 1
        ts = task_started[0]
        assert ts["backend"] == "claude"
        assert ts["level"] == "info"
        assert isinstance(ts["data"]["turn_id"], str)
        assert isinstance(ts["data"]["started_at_ms"], int)
        # The same turn_id surfaces on the closing agent.result.
        assert events[-1].get("turn_id") == ts["data"]["turn_id"]
    finally:
        await proc.close()


async def test_task_started_notice_is_emitted_after_system_init(monkeypatch):
    """Frame order matches codex: agent.system_init lands first, then
    the synth agent.notice{task_started}, then the content events.

    Codex emits `session_configured` → `task_started` natively; for
    parity the daemon defers Claude's synth notice until CC's first
    stdout event (`system{subtype:"init"}`) has been forwarded.
    """
    monkeypatch.setenv("BLEMEES_FAKE_MODE", "normal")
    queue: asyncio.Queue = asyncio.Queue()
    logger = configure("error")
    proc = ClaudeBackend(
        session_id="s1",
        argv=_make_argv("s1"),
        cwd=None,
        on_event=queue.put,
        logger=logger,
    )
    await proc.spawn()
    try:
        await proc.send_user_turn({"role": "user", "content": "hi"})
        events = await _drain_until_result(queue, "s1")
        # Find positions; both must be present.
        init_idx = next(i for i, e in enumerate(events) if e["type"] == "agent.system_init")
        ts_idx = next(
            i
            for i, e in enumerate(events)
            if e["type"] == "agent.notice" and e.get("category") == "task_started"
        )
        assert init_idx < ts_idx, (
            f"agent.system_init (idx={init_idx}) must precede task_started "
            f"(idx={ts_idx}) — see docs/asymmetries.md item 6"
        )
    finally:
        await proc.close()


async def test_system_init_carries_capabilities(monkeypatch, tmp_path):
    """Capabilities are synthesised from options.claude.* so claude's
    system_init parallels codex's shape."""
    monkeypatch.setenv("BLEMEES_FAKE_MODE", "normal")
    monkeypatch.setenv("HOME", str(tmp_path))
    queue: asyncio.Queue = asyncio.Queue()
    logger = configure("error")
    cwd = tmp_path / "proj"
    cwd.mkdir()
    options = {
        "tools": "",
        "permission_mode": "bypassPermissions",
        "effort": "high",
    }
    proc = ClaudeBackend(
        session_id="s1",
        argv=build_argv(FAKE_CLAUDE, session_id="s1", options=options, for_resume=False),
        cwd=str(cwd),
        options=options,
        on_event=queue.put,
        logger=logger,
    )
    await proc.spawn()
    try:
        await proc.send_user_turn({"role": "user", "content": "hi"})
        events = await _drain_until_result(queue, "s1")
        init = next(e for e in events if e["type"] == "agent.system_init")
        caps = init["capabilities"]
        assert caps["permission_mode"] == "bypassPermissions"
        assert caps["reasoning_effort"] == "high"  # renamed from `effort` for codex parity
        # rollout_path mirrors codex.session_configured.rollout_path.
        assert caps["rollout_path"].endswith("/s1.jsonl")
        assert "/.claude/projects/" in caps["rollout_path"]
    finally:
        await proc.close()


async def test_crash_mid_turn_synthesises_agent_result(monkeypatch):
    """When the subprocess crashes mid-turn, the daemon synthesises a
    closing agent.result{subtype:"error"} so clients waiting on
    agent.result aren't left hanging (spec §5.6)."""
    monkeypatch.setenv("BLEMEES_FAKE_MODE", "crash")
    queue: asyncio.Queue = asyncio.Queue()
    logger = configure("error")
    proc = ClaudeBackend(
        session_id="s1",
        argv=_make_argv("s1"),
        cwd=None,
        on_event=queue.put,
        logger=logger,
    )
    await proc.spawn()
    try:
        await proc.send_user_turn({"role": "user", "content": "boom"})
        saw_error_frame = False
        saw_synth_result = False
        for _ in range(40):
            evt = await asyncio.wait_for(queue.get(), timeout=5.0)
            if evt.get("type") == "blemeesd.error" and evt.get("code") == "backend_crashed":
                saw_error_frame = True
            if evt.get("type") == "agent.result" and evt.get("subtype") == "error":
                saw_synth_result = True
                # Synth result carries the turn_id allocated at send_user_turn.
                assert isinstance(evt.get("turn_id"), str)
                assert evt["error"]["code"] == "backend_crashed"
                assert "stderr tail" in evt["error"]["message"]
                break
        assert saw_error_frame, "blemeesd.error{backend_crashed} not emitted"
        assert saw_synth_result, "synth agent.result{error} not emitted"
    finally:
        await proc.close()


async def test_auth_failure_mid_turn_synthesises_agent_result(monkeypatch):
    """Mid-turn auth failure closes the turn cleanly with a synthesised
    agent.result{subtype:"error", error.code:"auth_failed"}."""
    monkeypatch.setenv("BLEMEES_FAKE_MODE", "oauth_midturn")
    queue: asyncio.Queue = asyncio.Queue()
    logger = configure("error")
    proc = ClaudeBackend(
        session_id="s1",
        argv=_make_argv("s1"),
        cwd=None,
        on_event=queue.put,
        logger=logger,
    )
    await proc.spawn()
    try:
        await proc.send_user_turn({"role": "user", "content": "boom"})
        saw_auth = False
        saw_synth = False
        for _ in range(40):
            evt = await asyncio.wait_for(queue.get(), timeout=5.0)
            if evt.get("code") == "auth_failed":
                saw_auth = True
            if evt.get("type") == "agent.result" and evt.get("subtype") == "error":
                if evt.get("error", {}).get("code") == "auth_failed":
                    saw_synth = True
                    assert isinstance(evt.get("turn_id"), str)
                    break
        assert saw_auth, "blemeesd.error{auth_failed} not emitted"
        assert saw_synth, "synth agent.result{error} for auth not emitted"
    finally:
        await proc.close()


async def test_interrupt_synth_result_carries_turn_metadata(monkeypatch):
    """The synthesised agent.result{interrupted} stamps the per-turn
    metadata (turn_id, time_to_first_token_ms when a delta was seen)."""
    monkeypatch.setenv("BLEMEES_FAKE_MODE", "slow")
    queue: asyncio.Queue = asyncio.Queue()
    logger = configure("error")
    proc = ClaudeBackend(
        session_id="s1",
        argv=_make_argv("s1"),
        cwd=None,
        on_event=queue.put,
        logger=logger,
    )
    await proc.spawn()
    try:
        await proc.send_user_turn({"role": "user", "content": "go"})
        # Drain until we've seen at least one delta, so first-token time is set.
        seen_delta = False
        while not seen_delta:
            evt = await asyncio.wait_for(queue.get(), timeout=5.0)
            if evt.get("type") == "agent.delta":
                seen_delta = True
        await proc.interrupt()
        # Wait for the synthesised agent.result.
        for _ in range(20):
            evt = await asyncio.wait_for(queue.get(), timeout=5.0)
            if evt.get("type") == "agent.result" and evt.get("subtype") == "interrupted":
                assert isinstance(evt.get("turn_id"), str)
                # We saw a delta, so TTFT must have been measured.
                assert isinstance(evt.get("time_to_first_token_ms"), int)
                assert evt["time_to_first_token_ms"] >= 0
                break
        else:  # pragma: no cover - test diagnostic
            raise AssertionError("interrupted result never arrived")
        monkeypatch.setenv("BLEMEES_FAKE_MODE", "normal")
    finally:
        await proc.close()


async def test_session_busy_while_turn_in_flight(monkeypatch):
    monkeypatch.setenv("BLEMEES_FAKE_MODE", "slow")
    queue: asyncio.Queue = asyncio.Queue()
    logger = configure("error")
    proc = ClaudeBackend(
        session_id="s1",
        argv=_make_argv("s1"),
        cwd=None,
        on_event=queue.put,
        logger=logger,
    )
    await proc.spawn()
    try:
        await proc.send_user_turn({"role": "user", "content": "go"})
        # Wait for at least one delta so we know the turn is live.
        while True:
            evt = await asyncio.wait_for(queue.get(), timeout=5.0)
            if evt.get("type") == "agent.delta":
                break
        with pytest.raises(SessionBusyError):
            await proc.send_user_turn({"role": "user", "content": "again"})
    finally:
        await proc.close()


async def test_interrupt_kills_and_respawns_with_resume(monkeypatch):
    monkeypatch.setenv("BLEMEES_FAKE_MODE", "slow")
    queue: asyncio.Queue = asyncio.Queue()
    logger = configure("error")
    proc = ClaudeBackend(
        session_id="s1",
        argv=_make_argv("s1"),
        cwd=None,
        on_event=queue.put,
        logger=logger,
    )
    await proc.spawn()
    try:
        await proc.send_user_turn({"role": "user", "content": "go"})
        while True:
            evt = await asyncio.wait_for(queue.get(), timeout=5.0)
            if evt.get("type") == "agent.delta":
                break
        assert proc.turn_active is True
        did_kill = await proc.interrupt()
        assert did_kill is True
        assert "--resume" in proc._argv  # type: ignore[attr-defined]
        assert "--session-id" not in proc._argv  # type: ignore[attr-defined]
        assert proc.running is True
        monkeypatch.setenv("BLEMEES_FAKE_MODE", "normal")
    finally:
        await proc.close()


async def test_interrupt_noop_when_idle(monkeypatch):
    monkeypatch.setenv("BLEMEES_FAKE_MODE", "normal")
    queue: asyncio.Queue = asyncio.Queue()
    logger = configure("error")
    proc = ClaudeBackend(
        session_id="s1",
        argv=_make_argv("s1"),
        cwd=None,
        on_event=queue.put,
        logger=logger,
    )
    await proc.spawn()
    try:
        did_kill = await proc.interrupt()
        assert did_kill is False
    finally:
        await proc.close()


async def test_crash_surfaces_backend_crashed(monkeypatch):
    monkeypatch.setenv("BLEMEES_FAKE_MODE", "crash")
    queue: asyncio.Queue = asyncio.Queue()
    logger = configure("error")
    proc = ClaudeBackend(
        session_id="s1",
        argv=_make_argv("s1"),
        cwd=None,
        on_event=queue.put,
        logger=logger,
    )
    await proc.spawn()
    try:
        await proc.send_user_turn({"role": "user", "content": "boom"})
        saw_error = False
        for _ in range(20):
            evt = await asyncio.wait_for(queue.get(), timeout=5.0)
            if evt.get("type") == "blemeesd.error" and evt.get("code") == "backend_crashed":
                saw_error = True
                break
        assert saw_error, "never saw backend_crashed"
    finally:
        await proc.close()


async def test_auth_failure_detection(monkeypatch):
    monkeypatch.setenv("BLEMEES_FAKE_MODE", "oauth")
    queue: asyncio.Queue = asyncio.Queue()
    logger = configure("error")
    proc = ClaudeBackend(
        session_id="s1",
        argv=_make_argv("s1"),
        cwd=None,
        on_event=queue.put,
        logger=logger,
    )
    await proc.spawn()
    try:
        saw = False
        for _ in range(20):
            evt = await asyncio.wait_for(queue.get(), timeout=5.0)
            if evt.get("code") == "auth_failed":
                saw = True
                break
        assert saw, "auth_failed was never emitted"
    finally:
        await proc.close()


# ---------------------------------------------------------------------------
# build_user_stdin_line
# ---------------------------------------------------------------------------


def test_build_user_stdin_line_envelope_only():
    line = build_user_stdin_line("s1", message={"role": "user", "content": "hi"})
    obj = json.loads(line)
    assert obj == {"type": "user", "message": {"role": "user", "content": "hi"}, "session_id": "s1"}
    assert line.endswith(b"\n")


# ---------------------------------------------------------------------------
# list_on_disk_sessions
# ---------------------------------------------------------------------------


def _write_transcript(path: Path, *, first_user_text: str | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({"type": "system", "subtype": "init", "sessionId": path.stem}),
    ]
    if first_user_text is not None:
        lines.append(
            json.dumps(
                {
                    "type": "user",
                    "message": {"role": "user", "content": first_user_text},
                    "sessionId": path.stem,
                }
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_list_on_disk_sessions_empty_when_no_project_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert list_on_disk_sessions("/some/where/else") == []


def test_list_on_disk_sessions_reads_metadata_and_preview(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cwd = "/home/u/proj"
    project_dir = project_dir_for_cwd(cwd)
    assert project_dir == tmp_path / ".claude" / "projects" / "-home-u-proj"

    _write_transcript(project_dir / "aaa.jsonl", first_user_text="First message")
    _write_transcript(project_dir / "bbb.jsonl", first_user_text="Second session")

    import os

    os.utime(project_dir / "aaa.jsonl", (1_700_000_000, 1_700_000_000))
    os.utime(project_dir / "bbb.jsonl", (1_700_000_100, 1_700_000_100))

    rows = list_on_disk_sessions(cwd)
    assert [r["session_id"] for r in rows] == ["bbb", "aaa"]
    assert rows[0]["mtime_ms"] == 1_700_000_100_000
    assert rows[0]["size"] > 0
    assert rows[0]["preview"] == "Second session"
    assert rows[1]["preview"] == "First message"


def test_list_on_disk_sessions_omits_preview_when_no_user_line(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cwd = "/p"
    project_dir = project_dir_for_cwd(cwd)
    _write_transcript(project_dir / "xxx.jsonl", first_user_text=None)
    rows = list_on_disk_sessions(cwd)
    assert len(rows) == 1
    assert rows[0]["session_id"] == "xxx"
    assert "preview" not in rows[0]


def test_list_on_disk_sessions_preview_caps_length(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cwd = "/p"
    project_dir = project_dir_for_cwd(cwd)
    _write_transcript(project_dir / "big.jsonl", first_user_text="x" * 500)
    rows = list_on_disk_sessions(cwd)
    assert len(rows[0]["preview"]) == 200


def test_list_on_disk_sessions_supports_content_blocks(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cwd = "/p"
    project_dir = project_dir_for_cwd(cwd)
    project_dir.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64"}},
                    {"type": "text", "text": "describe this"},
                ],
            },
        }
    )
    (project_dir / "img.jsonl").write_text(line + "\n", encoding="utf-8")
    rows = list_on_disk_sessions(cwd)
    assert rows[0]["preview"] == "describe this"
