"""Unit tests for the ClaudeSubprocess wrapper using the fake claude stub."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from ccsock.errors import SessionBusyError
from ccsock.logging import configure
from ccsock.protocol import OpenMessage, build_claude_argv, build_user_stdin_line
from ccsock.subprocess import (
    ClaudeSubprocess,
    _argv_to_resume,
    list_session_files,
    project_dir_for_cwd,
)


FAKE_CLAUDE = str(Path(__file__).parent / "fake_claude.py")


def _open_msg(session: str = "s1") -> OpenMessage:
    return OpenMessage(
        id=None,
        session=session,
        resume=False,
        fields={"session": session, "tools": ""},
    )


def _make_argv(session: str, *, for_resume: bool = False) -> list[str]:
    return build_claude_argv(FAKE_CLAUDE, _open_msg(session), for_resume=for_resume)


def test_argv_to_resume_rewrites():
    argv = ["claude", "-p", "--session-id", "abc", "--input-format", "x"]
    out = _argv_to_resume(argv, "abc")
    assert "--resume" in out and "--session-id" not in out
    assert out[out.index("--resume") + 1] == "abc"


def test_argv_to_resume_is_idempotent_when_already_resume():
    argv = ["claude", "-p", "--resume", "abc"]
    out = _argv_to_resume(argv, "abc")
    assert out.count("--resume") == 1


async def _drain_until_result(queue: asyncio.Queue, session: str) -> list[dict]:
    events = []
    while True:
        evt = await asyncio.wait_for(queue.get(), timeout=5.0)
        events.append(evt)
        if evt.get("type") == "result" and evt.get("session") == session:
            return events


async def test_normal_turn_produces_result(monkeypatch):
    monkeypatch.setenv("CCSOCK_FAKE_MODE", "normal")
    queue: asyncio.Queue = asyncio.Queue()
    logger = configure("error")
    proc = ClaudeSubprocess(
        session_id="s1",
        argv=_make_argv("s1"),
        cwd=None,
        event_queue=queue,
        logger=logger,
    )
    await proc.spawn()
    try:
        assert proc.running is True
        line = build_user_stdin_line("s1", text="hi", content=None)
        await proc.send_user_line(line)
        events = await _drain_until_result(queue, "s1")
        kinds = [e["type"] for e in events]
        assert "system" in kinds
        assert "stream_event" in kinds
        assert "assistant" in kinds
        assert events[-1]["type"] == "result"
        assert events[-1]["session"] == "s1"
        assert proc.turn_active is False
    finally:
        await proc.close()


async def test_session_busy_while_turn_in_flight(monkeypatch):
    monkeypatch.setenv("CCSOCK_FAKE_MODE", "slow")
    queue: asyncio.Queue = asyncio.Queue()
    logger = configure("error")
    proc = ClaudeSubprocess(
        session_id="s1",
        argv=_make_argv("s1"),
        cwd=None,
        event_queue=queue,
        logger=logger,
    )
    await proc.spawn()
    try:
        await proc.send_user_line(build_user_stdin_line("s1", text="go", content=None))
        # Wait for at least one delta so we know the turn is live.
        while True:
            evt = await asyncio.wait_for(queue.get(), timeout=5.0)
            if evt.get("type") == "stream_event":
                break
        with pytest.raises(SessionBusyError):
            await proc.send_user_line(
                build_user_stdin_line("s1", text="again", content=None)
            )
    finally:
        await proc.close()


async def test_interrupt_kills_and_respawns_with_resume(monkeypatch):
    monkeypatch.setenv("CCSOCK_FAKE_MODE", "slow")
    queue: asyncio.Queue = asyncio.Queue()
    logger = configure("error")
    proc = ClaudeSubprocess(
        session_id="s1",
        argv=_make_argv("s1"),
        cwd=None,
        event_queue=queue,
        logger=logger,
    )
    await proc.spawn()
    try:
        await proc.send_user_line(build_user_stdin_line("s1", text="go", content=None))
        # Wait for streaming to start.
        while True:
            evt = await asyncio.wait_for(queue.get(), timeout=5.0)
            if evt.get("type") == "stream_event":
                break
        assert proc.turn_active is True
        did_kill = await proc.interrupt()
        assert did_kill is True
        # After respawn, argv must contain --resume, not --session-id.
        assert "--resume" in proc._argv  # type: ignore[attr-defined]
        assert "--session-id" not in proc._argv  # type: ignore[attr-defined]
        assert proc.running is True

        # Subsequent turn works (switch mode to normal for a clean reply).
        monkeypatch.setenv("CCSOCK_FAKE_MODE", "normal")
    finally:
        await proc.close()


async def test_interrupt_noop_when_idle(monkeypatch):
    monkeypatch.setenv("CCSOCK_FAKE_MODE", "normal")
    queue: asyncio.Queue = asyncio.Queue()
    logger = configure("error")
    proc = ClaudeSubprocess(
        session_id="s1",
        argv=_make_argv("s1"),
        cwd=None,
        event_queue=queue,
        logger=logger,
    )
    await proc.spawn()
    try:
        did_kill = await proc.interrupt()
        assert did_kill is False
    finally:
        await proc.close()


async def test_crash_surfaces_claude_crashed(monkeypatch):
    monkeypatch.setenv("CCSOCK_FAKE_MODE", "crash")
    queue: asyncio.Queue = asyncio.Queue()
    logger = configure("error")
    proc = ClaudeSubprocess(
        session_id="s1",
        argv=_make_argv("s1"),
        cwd=None,
        event_queue=queue,
        logger=logger,
    )
    await proc.spawn()
    try:
        await proc.send_user_line(build_user_stdin_line("s1", text="boom", content=None))
        saw_error = False
        for _ in range(20):
            evt = await asyncio.wait_for(queue.get(), timeout=5.0)
            if evt.get("type") == "ccsockd.error" and evt.get("code") == "claude_crashed":
                saw_error = True
                break
        assert saw_error, "never saw claude_crashed"
    finally:
        await proc.close()


async def test_oauth_detection(monkeypatch):
    monkeypatch.setenv("CCSOCK_FAKE_MODE", "oauth")
    queue: asyncio.Queue = asyncio.Queue()
    logger = configure("error")
    proc = ClaudeSubprocess(
        session_id="s1",
        argv=_make_argv("s1"),
        cwd=None,
        event_queue=queue,
        logger=logger,
    )
    await proc.spawn()
    try:
        saw = False
        for _ in range(20):
            evt = await asyncio.wait_for(queue.get(), timeout=5.0)
            if evt.get("code") == "oauth_expired":
                saw = True
                break
        assert saw, "oauth_expired was never emitted"
    finally:
        await proc.close()


# ---------------------------------------------------------------------------
# list_session_files
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


def test_list_session_files_empty_when_no_project_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert list_session_files("/some/where/else") == []


def test_list_session_files_reads_metadata_and_preview(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cwd = "/home/u/proj"
    project_dir = project_dir_for_cwd(cwd)
    assert project_dir == tmp_path / ".claude" / "projects" / "-home-u-proj"

    _write_transcript(project_dir / "aaa.jsonl", first_user_text="First message")
    _write_transcript(project_dir / "bbb.jsonl", first_user_text="Second session")

    # Touch files to set deterministic mtimes: bbb newer than aaa.
    import os
    os.utime(project_dir / "aaa.jsonl", (1_700_000_000, 1_700_000_000))
    os.utime(project_dir / "bbb.jsonl", (1_700_000_100, 1_700_000_100))

    rows = list_session_files(cwd)
    assert [r["session"] for r in rows] == ["bbb", "aaa"]
    assert rows[0]["mtime_ms"] == 1_700_000_100_000
    assert rows[0]["size"] > 0
    assert rows[0]["preview"] == "Second session"
    assert rows[1]["preview"] == "First message"


def test_list_session_files_omits_preview_when_no_user_line(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cwd = "/p"
    project_dir = project_dir_for_cwd(cwd)
    _write_transcript(project_dir / "xxx.jsonl", first_user_text=None)
    rows = list_session_files(cwd)
    assert len(rows) == 1
    assert rows[0]["session"] == "xxx"
    assert "preview" not in rows[0]


def test_list_session_files_preview_caps_length(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cwd = "/p"
    project_dir = project_dir_for_cwd(cwd)
    _write_transcript(project_dir / "big.jsonl", first_user_text="x" * 500)
    rows = list_session_files(cwd)
    assert len(rows[0]["preview"]) == 200


def test_list_session_files_supports_content_blocks(tmp_path, monkeypatch):
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
    rows = list_session_files(cwd)
    assert rows[0]["preview"] == "describe this"
