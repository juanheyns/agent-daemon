"""Unit tests for the CodexBackend wrapper using the fake codex stub."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from blemees_agent.backends.codex import (
    CodexBackend,
    _looks_like_auth_failure,
    build_argv,
    build_codex_tool_args,
    detect_version,
    list_on_disk_sessions,
    validate_options,
)
from blemees_agent.errors import ProtocolError, SessionBusyError
from blemees_agent.logging import configure

FAKE_CODEX = str(Path(__file__).parent / "fake_codex.py")


def _make_backend(
    queue: asyncio.Queue,
    *,
    session_id: str = "s1",
    options: dict | None = None,
    include_raw_events: bool = False,
    thread_id: str | None = None,
) -> CodexBackend:
    logger = configure("error")
    options = options or {}
    argv = build_argv(FAKE_CODEX, options=options)
    return CodexBackend(
        session_id=session_id,
        argv=argv,
        cwd=None,
        options=options,
        on_event=queue.put,
        logger=logger,
        include_raw_events=include_raw_events,
        thread_id=thread_id,
    )


async def _drain_until_result(queue: asyncio.Queue) -> list[dict]:
    events: list[dict] = []
    while True:
        evt = await asyncio.wait_for(queue.get(), timeout=10.0)
        events.append(evt)
        if evt.get("type") == "agent.result":
            return events


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_build_argv_passes_config_pairs():
    argv = build_argv(
        "/usr/bin/codex",
        options={"config": {"model_provider": "openai", "verbose": True}},
    )
    assert argv[:2] == ["/usr/bin/codex", "mcp-server"]
    assert "-c" in argv
    cmds = [argv[i + 1] for i, a in enumerate(argv) if a == "-c"]
    assert "model_provider=openai" in cmds
    assert "verbose=true" in cmds


def test_build_argv_handles_features_map():
    argv = build_argv(
        "codex",
        options={"config": {"features": {"alpha": True, "legacy": False}}},
    )
    assert argv[:2] == ["codex", "mcp-server"]
    # Features become --enable / --disable, not -c entries.
    assert "--enable" in argv and "alpha" in argv
    assert "--disable" in argv and "legacy" in argv


def test_build_codex_tool_args_passes_through_known_keys():
    args = build_codex_tool_args(
        {
            "model": "gpt-5.2-codex",
            "sandbox": "read-only",
            "approval-policy": "never",
            "config": {"foo": "bar"},  # not a tool-call key
            "include_raw_events": True,  # daemon-side flag
        },
        prompt="hi",
    )
    assert args == {
        "prompt": "hi",
        "model": "gpt-5.2-codex",
        "sandbox": "read-only",
        "approval-policy": "never",
    }


def test_validate_options_rejects_unknown_keys():
    with pytest.raises(ProtocolError):
        validate_options({"not_a_real_key": True})


def test_validate_options_accepts_known_keys():
    validate_options(
        {
            "model": "x",
            "sandbox": "read-only",
            "config": {},
            "include_raw_events": True,
        }
    )


# ---------------------------------------------------------------------------
# Live subprocess interactions (against the fake codex stub).
# ---------------------------------------------------------------------------


async def test_normal_turn_produces_result(monkeypatch):
    monkeypatch.setenv("BLEMEES_FAKE_MODE", "normal")
    queue: asyncio.Queue = asyncio.Queue()
    proc = _make_backend(queue)
    await proc.spawn()
    try:
        assert proc.running is True
        await proc.send_user_turn({"role": "user", "content": "hi"})
        events = await _drain_until_result(queue)
        kinds = [e["type"] for e in events]
        assert "agent.system_init" in kinds
        assert "agent.delta" in kinds
        assert "agent.message" in kinds
        result = events[-1]
        assert result["type"] == "agent.result"
        assert result["subtype"] == "success"
        assert result["backend"] == "codex"
        assert result["usage"]["input_tokens"] == 10
        # Codex's `cached_input_tokens` renamed to the unified key.
        assert result["usage"]["cache_read_input_tokens"] == 4
        assert result["usage"]["reasoning_output_tokens"] == 2
        # `user_echo` defaults to False — the fake's
        # item_completed{UserMessage} is suppressed by the translator,
        # symmetric with claude's default.
        assert "agent.user_echo" not in kinds
        assert proc.turn_active is False
    finally:
        await proc.close()


async def test_user_echo_default_off_drops_user_message_item(monkeypatch):
    """Default-off symmetry with claude: codex's item_completed{UserMessage}
    is dropped from the primary stream unless `options.codex.user_echo`
    is set."""
    monkeypatch.setenv("BLEMEES_FAKE_MODE", "normal")
    queue: asyncio.Queue = asyncio.Queue()
    proc = _make_backend(queue)  # default options → user_echo absent → False
    await proc.spawn()
    try:
        await proc.send_user_turn({"role": "user", "content": "hi"})
        events = await _drain_until_result(queue)
        assert not any(e.get("type") == "agent.user_echo" for e in events)
    finally:
        await proc.close()


async def test_user_echo_true_emits_user_message_item(monkeypatch):
    """Opt-in: with `options.codex.user_echo=True` the translator
    forwards item_completed{UserMessage} as agent.user_echo."""
    monkeypatch.setenv("BLEMEES_FAKE_MODE", "normal")
    queue: asyncio.Queue = asyncio.Queue()
    proc = _make_backend(queue, options={"user_echo": True})
    await proc.spawn()
    try:
        await proc.send_user_turn({"role": "user", "content": "hi"})
        events = await _drain_until_result(queue)
        echoes = [e for e in events if e.get("type") == "agent.user_echo"]
        assert len(echoes) == 1
        assert echoes[0]["message"]["role"] == "user"
        assert echoes[0]["message"]["content"] == [{"type": "text", "text": "hi"}]
    finally:
        await proc.close()


async def test_first_turn_uses_codex_then_codex_reply(monkeypatch, tmp_path):
    monkeypatch.setenv("BLEMEES_FAKE_MODE", "normal")
    # Capture every JSON-RPC line the backend writes by intercepting
    # stdin via a wrapper. Simpler: just spawn and send two turns and
    # assert the second turn produced a result with backend=codex.
    queue: asyncio.Queue = asyncio.Queue()
    proc = _make_backend(queue)
    await proc.spawn()
    try:
        await proc.send_user_turn({"role": "user", "content": "first"})
        first = await _drain_until_result(queue)
        assert first[-1]["subtype"] == "success"
        assert proc._thread_id is not None  # cached after first turn

        await proc.send_user_turn({"role": "user", "content": "second"})
        second = await _drain_until_result(queue)
        assert second[-1]["subtype"] == "success"
        # No `agent.system_init` on the second turn — the fake only
        # sends `session_configured` for the first call.
        kinds = [e["type"] for e in second]
        assert "agent.system_init" not in kinds
    finally:
        await proc.close()


async def test_session_busy_while_turn_in_flight(monkeypatch):
    monkeypatch.setenv("BLEMEES_FAKE_MODE", "slow")
    queue: asyncio.Queue = asyncio.Queue()
    proc = _make_backend(queue)
    await proc.spawn()
    try:
        await proc.send_user_turn({"role": "user", "content": "go"})
        # Wait for at least one delta so the turn is definitely live.
        while True:
            evt = await asyncio.wait_for(queue.get(), timeout=5.0)
            if evt.get("type") == "agent.delta":
                break
        with pytest.raises(SessionBusyError):
            await proc.send_user_turn({"role": "user", "content": "again"})
    finally:
        await proc.close()


async def test_non_text_content_is_rejected(monkeypatch):
    monkeypatch.setenv("BLEMEES_FAKE_MODE", "normal")
    queue: asyncio.Queue = asyncio.Queue()
    proc = _make_backend(queue)
    await proc.spawn()
    try:
        with pytest.raises(ProtocolError):
            await proc.send_user_turn(
                {
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64"}},
                    ],
                }
            )
        # Backend should NOT have flipped turn_active for the rejected message.
        assert proc.turn_active is False
    finally:
        await proc.close()


async def test_auth_error_emits_auth_failed(monkeypatch):
    monkeypatch.setenv("BLEMEES_FAKE_MODE", "auth")
    queue: asyncio.Queue = asyncio.Queue()
    proc = _make_backend(queue)
    await proc.spawn()
    try:
        await proc.send_user_turn({"role": "user", "content": "hi"})
        saw_auth = False
        saw_result = False
        for _ in range(20):
            evt = await asyncio.wait_for(queue.get(), timeout=5.0)
            if evt.get("type") == "blemeesd.error" and evt.get("code") == "auth_failed":
                saw_auth = True
            if evt.get("type") == "agent.result":
                assert evt["subtype"] == "error"
                saw_result = True
                break
        assert saw_auth, "auth_failed never emitted"
        assert saw_result, "agent.result error never emitted"
    finally:
        await proc.close()


async def test_crash_surfaces_backend_crashed(monkeypatch):
    """Crash mid-turn surfaces both `blemeesd.error{backend_crashed}` and
    a synthesised closing `agent.result{subtype:"error"}` so the turn
    invariant from spec §5.6 holds (mirrors the Claude backend)."""
    monkeypatch.setenv("BLEMEES_FAKE_MODE", "crash")
    queue: asyncio.Queue = asyncio.Queue()
    proc = _make_backend(queue)
    await proc.spawn()
    try:
        await proc.send_user_turn({"role": "user", "content": "boom"})
        saw_error = False
        saw_synth_result = False
        for _ in range(40):
            evt = await asyncio.wait_for(queue.get(), timeout=5.0)
            if evt.get("type") == "blemeesd.error" and evt.get("code") == "backend_crashed":
                saw_error = True
            if evt.get("type") == "agent.result" and evt.get("subtype") == "error":
                saw_synth_result = True
                assert evt["error"]["code"] == "backend_crashed"
                assert "stderr tail" in evt["error"]["message"]
                break
        assert saw_error, "never saw backend_crashed"
        assert saw_synth_result, "never saw synth agent.result{error}"
    finally:
        await proc.close()


async def test_interrupt_marks_turn_interrupted(monkeypatch):
    monkeypatch.setenv("BLEMEES_FAKE_MODE", "slow")
    queue: asyncio.Queue = asyncio.Queue()
    proc = _make_backend(queue)
    await proc.spawn()
    try:
        await proc.send_user_turn({"role": "user", "content": "go"})
        while True:
            evt = await asyncio.wait_for(queue.get(), timeout=5.0)
            if evt.get("type") == "agent.delta":
                break
        did_kill = await proc.interrupt()
        assert did_kill is True
        # Drain until we see agent.result — should have subtype=interrupted.
        for _ in range(50):
            evt = await asyncio.wait_for(queue.get(), timeout=5.0)
            if evt.get("type") == "agent.result":
                assert evt["subtype"] == "interrupted"
                break
        else:  # pragma: no cover - defensive
            pytest.fail("no agent.result after interrupt")
    finally:
        await proc.close()


async def test_interrupt_noop_when_idle(monkeypatch):
    monkeypatch.setenv("BLEMEES_FAKE_MODE", "normal")
    queue: asyncio.Queue = asyncio.Queue()
    proc = _make_backend(queue)
    await proc.spawn()
    try:
        did_kill = await proc.interrupt()
        assert did_kill is False
    finally:
        await proc.close()


async def test_include_raw_events_carries_raw_payload(monkeypatch):
    monkeypatch.setenv("BLEMEES_FAKE_MODE", "normal")
    queue: asyncio.Queue = asyncio.Queue()
    proc = _make_backend(queue, include_raw_events=True)
    await proc.spawn()
    try:
        await proc.send_user_turn({"role": "user", "content": "hi"})
        events = await _drain_until_result(queue)
        # Find a delta with raw payload — it should carry the codex msg
        # under raw and the _meta we extracted from the notification.
        deltas = [e for e in events if e.get("type") == "agent.delta"]
        assert deltas
        assert "raw" in deltas[0]
        assert deltas[0]["raw"]["type"] == "agent_message_content_delta"
        assert deltas[0]["raw"]["_meta"]["threadId"]
    finally:
        await proc.close()


# ---------------------------------------------------------------------------
# detect_version
# ---------------------------------------------------------------------------


def test_detect_version_reads_codex_cli_format(monkeypatch, tmp_path):
    # Write a stub script that prints the canonical "codex-cli X.Y.Z" line.
    stub = tmp_path / "codex"
    stub.write_text("#!/bin/sh\necho 'codex-cli 0.999.0'\n", encoding="utf-8")
    stub.chmod(0o755)
    assert detect_version(str(stub)) == "0.999.0"


def test_detect_version_returns_none_when_missing(tmp_path):
    assert detect_version(str(tmp_path / "no-such-binary")) is None


# ---------------------------------------------------------------------------
# Auth-error classifier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "err",
    [
        {"code": -32001, "message": "auth required"},
        {"code": -32002, "message": "upstream returned 401"},
        {"code": 401, "message": "Unauthorized"},
        {"code": 403, "message": "Forbidden"},
        {"code": -32603, "message": "401 Unauthorized: please run `codex login`"},
        {"code": -32603, "message": "OPENAI_API_KEY missing"},
        {"code": -32603, "data": {"code": 401}, "message": "boom"},
        {"code": -32603, "data": {"type": "auth_failed"}, "message": "x"},
        {"code": -32603, "data": {"type": "Unauthorized"}, "message": "x"},
    ],
)
def test_looks_like_auth_failure_positives(err):
    assert _looks_like_auth_failure(err) is True


@pytest.mark.parametrize(
    "err",
    [
        {"code": -32603, "message": "internal error"},
        {"code": -32602, "message": "invalid params"},
        {"code": -32000, "message": "rate limited"},
        {"message": "no code at all"},
        {},
    ],
)
def test_looks_like_auth_failure_negatives(err):
    assert _looks_like_auth_failure(err) is False


# ---------------------------------------------------------------------------
# Phase 4: resume routes the first turn through `codex-reply`
# ---------------------------------------------------------------------------


async def test_resume_with_thread_id_uses_codex_reply(monkeypatch, tmp_path):
    monkeypatch.setenv("BLEMEES_FAKE_MODE", "normal")
    rpc_log = tmp_path / "rpc.jsonl"
    monkeypatch.setenv("BLEMEES_FAKE_RPC_LOG", str(rpc_log))

    queue: asyncio.Queue = asyncio.Queue()
    proc = _make_backend(queue, thread_id="cached-thread-xyz")
    await proc.spawn()
    try:
        await proc.send_user_turn({"role": "user", "content": "hi"})
        await _drain_until_result(queue)
    finally:
        await proc.close()

    lines = [ln for ln in rpc_log.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert lines, "rpc log empty"
    first_call = json.loads(lines[0])
    assert first_call["tool"] == "codex-reply"
    assert first_call["thread_id"] == "cached-thread-xyz"


async def test_fresh_session_uses_codex_for_first_turn(monkeypatch, tmp_path):
    monkeypatch.setenv("BLEMEES_FAKE_MODE", "normal")
    rpc_log = tmp_path / "rpc.jsonl"
    monkeypatch.setenv("BLEMEES_FAKE_RPC_LOG", str(rpc_log))

    queue: asyncio.Queue = asyncio.Queue()
    proc = _make_backend(queue)  # no thread_id
    await proc.spawn()
    try:
        await proc.send_user_turn({"role": "user", "content": "hi"})
        await _drain_until_result(queue)
        await proc.send_user_turn({"role": "user", "content": "again"})
        await _drain_until_result(queue)
    finally:
        await proc.close()

    lines = [
        json.loads(ln) for ln in rpc_log.read_text(encoding="utf-8").splitlines() if ln.strip()
    ]
    assert lines[0]["tool"] == "codex"
    assert lines[1]["tool"] == "codex-reply"
    # threadId carried into the second call comes from session_configured.
    assert lines[1]["thread_id"]


# ---------------------------------------------------------------------------
# Phase 4: on-disk discovery
# ---------------------------------------------------------------------------


def _write_rollout(
    sessions_root: Path,
    *,
    date_path: tuple[str, str, str],
    timestamp: str,
    thread_id: str,
    cwd: str,
    user_text: str | None = None,
) -> Path:
    """Write a fake rollout file mirroring Codex's on-disk layout."""
    y, m, d = date_path
    day_dir = sessions_root / y / m / d
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / f"rollout-{timestamp}-{thread_id}.jsonl"
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
    return path


def test_list_on_disk_sessions_empty_when_root_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert list_on_disk_sessions("/some/cwd") == []


def test_list_on_disk_sessions_filters_by_cwd(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    root = tmp_path / ".codex" / "sessions"

    target = _write_rollout(
        root,
        date_path=("2026", "04", "27"),
        timestamp="2026-04-27T14-42-22",
        thread_id="019dd03f-e946-7dd3-a0e4-3a3db8146dae",
        cwd="/work/blemees",
        user_text="first turn please",
    )
    _write_rollout(
        root,
        date_path=("2026", "04", "27"),
        timestamp="2026-04-27T15-01-00",
        thread_id="019dd040-aaaa-bbbb-cccc-ddddddddeeee",
        cwd="/work/other-repo",
    )

    rows = list_on_disk_sessions("/work/blemees")
    assert [r["session_id"] for r in rows] == ["019dd03f-e946-7dd3-a0e4-3a3db8146dae"]
    assert rows[0]["rollout_path"] == str(target)
    assert rows[0]["preview"] == "first turn please"
    assert rows[0]["mtime_ms"] > 0


def test_list_on_disk_sessions_newest_first(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    root = tmp_path / ".codex" / "sessions"

    older = _write_rollout(
        root,
        date_path=("2026", "04", "20"),
        timestamp="2026-04-20T10-00-00",
        thread_id="019dd000-aaaa-0000-0000-000000000000",
        cwd="/work/repo",
    )
    newer = _write_rollout(
        root,
        date_path=("2026", "04", "27"),
        timestamp="2026-04-27T14-42-22",
        thread_id="019dd03f-bbbb-0000-0000-000000000000",
        cwd="/work/repo",
    )
    import os

    os.utime(older, (1_700_000_000, 1_700_000_000))
    os.utime(newer, (1_700_001_000, 1_700_001_000))

    rows = list_on_disk_sessions("/work/repo")
    assert rows[0]["session_id"] == "019dd03f-bbbb-0000-0000-000000000000"
    assert rows[1]["session_id"] == "019dd000-aaaa-0000-0000-000000000000"


def test_list_on_disk_sessions_skips_non_rollout_entries(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    root = tmp_path / ".codex" / "sessions"

    _write_rollout(
        root,
        date_path=("2026", "04", "27"),
        timestamp="2026-04-27T14-42-22",
        thread_id="019dd03f-aaaa-0000-0000-000000000000",
        cwd="/x",
    )
    # Sibling file that doesn't match the rollout pattern.
    (root / "2026" / "04" / "27" / "stray.jsonl").write_text(
        json.dumps({"type": "session_configured", "cwd": "/x"}) + "\n",
        encoding="utf-8",
    )

    rows = list_on_disk_sessions("/x")
    assert len(rows) == 1
    assert rows[0]["session_id"] == "019dd03f-aaaa-0000-0000-000000000000"
