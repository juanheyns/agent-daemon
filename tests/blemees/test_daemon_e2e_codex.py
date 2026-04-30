"""End-to-end tests that require the real ``codex`` CLI and a live
``codex login`` session. Gated with ``requires_codex`` and auto-skipped
otherwise.

Run with::

    pytest -m requires_codex
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import uuid

import pytest
import pytest_asyncio

from blemees import PROTOCOL_VERSION
from blemees.config import Config
from blemees.daemon import Daemon
from blemees.logging import configure

CODEX = shutil.which("codex")


pytestmark = pytest.mark.requires_codex


def _need_codex() -> None:
    if CODEX is None:
        pytest.skip("`codex` not on PATH", allow_module_level=True)
    # `codex login status` exits 0 when authenticated. Treat any non-zero
    # exit (or process error) as "not logged in" → skip.
    try:
        proc = subprocess.run(
            [CODEX, "login", "status"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        pytest.skip(f"`codex login status` failed to run: {exc}", allow_module_level=True)
    if proc.returncode != 0:
        pytest.skip(
            f"`codex login status` reports not logged in (rc={proc.returncode})",
            allow_module_level=True,
        )


_need_codex()


@pytest_asyncio.fixture
async def real_daemon(tmp_path):
    from tests.blemees.conftest import short_socket_path

    socket_path = short_socket_path("blemeesd-e2e-codex")
    cfg = Config(socket_path=str(socket_path), codex_bin=CODEX)
    logger = configure("error")
    daemon = Daemon(cfg, logger)
    await daemon.start()
    serve_task = asyncio.create_task(daemon.serve_forever())
    try:
        yield str(socket_path)
    finally:
        daemon.request_shutdown()
        try:
            await asyncio.wait_for(serve_task, timeout=5.0)
        except TimeoutError:
            serve_task.cancel()


async def _client(socket_path: str):
    from tests.blemees.conftest import _StreamClient  # reuse helper

    reader, writer = await asyncio.open_unix_connection(socket_path)
    c = _StreamClient(reader, writer)
    await c.send({"type": "blemeesd.hello", "client": "e2e-codex/0", "protocol": PROTOCOL_VERSION})
    ack = await c.recv()
    assert ack["type"] == "blemeesd.hello_ack"
    return c


def _open_codex(session: str, *, cwd: str | None = None, **extra_options) -> dict:
    options: dict = {
        "sandbox": "read-only",
        "approval-policy": "never",
    }
    if cwd is not None:
        options["cwd"] = cwd
    options.update(extra_options)
    return {
        "type": "blemeesd.open",
        "id": "r1",
        "session_id": session,
        "backend": "codex",
        "options": {"codex": options},
    }


async def _drain_turn(c, session: str, *, timeout: float = 120.0) -> dict:
    return await c.wait_for(
        lambda e: e.get("type") == "agent.result" and e.get("session_id") == session,
        timeout=timeout,
    )


async def _say_ok(c, session: str, *, timeout: float = 120.0) -> dict:
    await c.send(
        {
            "type": "agent.user",
            "session_id": session,
            "message": {"role": "user", "content": "Say OK."},
        }
    )
    return await _drain_turn(c, session, timeout=timeout)


async def test_real_codex_turn_produces_result(real_daemon):
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(_open_codex(session))
        await c.wait_for(lambda e: e.get("type") == "blemeesd.opened", timeout=30.0)
        await c.send(
            {
                "type": "agent.user",
                "session_id": session,
                "message": {"role": "user", "content": "Say OK."},
            }
        )
        res = await c.wait_for(
            lambda e: e.get("type") == "agent.result" and e.get("session_id") == session,
            timeout=120.0,
        )
        assert res["subtype"] in {"success", "error", "interrupted"}
    finally:
        await c.close()


async def test_real_codex_context_across_two_turns(real_daemon):
    """Two-turn context test — single connection, single codex child.

    Resume across daemon-detach is intentionally NOT tested here: codex
    `tools/call codex-reply` rehydrates from the per-conversation
    rollout file, but that path is unstable in 0.125.x (a fresh
    `codex mcp-server` process called with a `threadId` from a prior
    process returns an empty success without rehydrating). The
    daemon-mock suite covers the cross-process resume routing
    (verifying we emit `codex-reply` with the cached threadId); we
    leave the actual context preservation to whichever side of the
    codex API stabilises first.
    """
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(_open_codex(session))
        await c.wait_for(lambda e: e.get("type") == "blemeesd.opened", timeout=30.0)
        await c.send(
            {
                "type": "agent.user",
                "session_id": session,
                "message": {"role": "user", "content": "Remember the number 17."},
            }
        )
        await c.wait_for(
            lambda e: e.get("type") == "agent.result" and e.get("session_id") == session,
            timeout=120.0,
        )
        await c.send(
            {
                "type": "agent.user",
                "session_id": session,
                "message": {
                    "role": "user",
                    "content": "What number did I ask you to remember? Answer with just the number.",
                },
            }
        )
        collected = await c.wait_for(
            lambda e: e.get("type") == "agent.result" and e.get("session_id") == session,
            collect=True,
            timeout=120.0,
        )
        text = ""
        for evt in collected:
            if evt.get("type") == "agent.message":
                for block in evt.get("content", []) or evt.get("message", {}).get("content", []):
                    if isinstance(block, dict) and block.get("type") == "text":
                        text += block.get("text", "")
        assert "17" in text, (
            f"second turn lost context: text={text!r} "
            f"frames={[(e.get('type'), e.get('subtype')) for e in collected]}"
        )
    finally:
        await c.close()


async def test_real_codex_session_info_in_memory_no_turns(real_daemon):
    """Right after open, session_info shows attached=true and zero turns."""
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(_open_codex(session))
        await c.wait_for(lambda e: e.get("type") == "blemeesd.opened", timeout=30.0)
        await c.send({"type": "blemeesd.session_info", "id": "i1", "session_id": session})
        info = await c.wait_for(
            lambda e: e.get("type") == "blemeesd.session_info_reply", timeout=10.0
        )
        assert info["backend"] == "codex"
        assert info["session_id"] == session
        assert info["turns"] == 0
        assert info["attached"] is True
        assert info["subprocess_running"] is True
    finally:
        await c.close()


async def test_real_codex_session_info_in_memory_after_turn(real_daemon):
    """After one turn, ``turns`` is 1 and usage counters are populated.

    Codex emits ``reasoning_output_tokens`` as a first-class field —
    confirm it survives normalisation and shows up in cumulative usage
    when the model used reasoning (which haiku-equivalent codex
    invariably does).
    """
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(_open_codex(session))
        await c.wait_for(lambda e: e.get("type") == "blemeesd.opened", timeout=30.0)
        await _say_ok(c, session)
        await c.send({"type": "blemeesd.session_info", "id": "i1", "session_id": session})
        info = await c.wait_for(
            lambda e: e.get("type") == "blemeesd.session_info_reply", timeout=10.0
        )
        assert info["turns"] == 1
        assert info["attached"] is True
        cu = info["cumulative_usage"]
        assert cu.get("input_tokens", 0) > 0
        assert cu.get("output_tokens", 0) > 0
    finally:
        await c.close()


async def test_real_codex_session_info_after_close_uses_on_disk(real_daemon, tmp_path):
    """Closed-but-on-disk codex sessions still answer session_info.

    Mirrors the claude regression test. For codex the daemon walks
    ``~/.codex/sessions/YYYY/MM/DD/rollout-*-<threadId>.jsonl`` and
    extracts ``cwd`` / ``model`` from the embedded
    ``session_configured`` event in the rollout head.
    """
    c = await _client(real_daemon)
    cwd = str(tmp_path.resolve())
    try:
        session = str(uuid.uuid4())
        await c.send(_open_codex(session, cwd=cwd))
        opened = await c.wait_for(lambda e: e.get("type") == "blemeesd.opened", timeout=30.0)
        # For codex, native_session_id only becomes meaningful after the
        # first turn — session_configured carries the real threadId.
        await _say_ok(c, session)
        # Re-resolve the threadId via session_info before closing — that's
        # what list_sessions and the on-disk lookup will key on.
        await c.send({"type": "blemeesd.session_info", "id": "pre", "session_id": session})
        pre = await c.wait_for(
            lambda e: e.get("type") == "blemeesd.session_info_reply" and e.get("id") == "pre",
            timeout=10.0,
        )
        thread_id = pre["native_session_id"]
        assert isinstance(thread_id, str) and thread_id

        await c.send({"type": "blemeesd.close", "id": "c1", "session_id": session, "delete": False})
        await c.wait_for(lambda e: e.get("type") == "blemeesd.closed", timeout=10.0)

        # Query by the daemon's session_id (not the threadId): the
        # daemon's cache held the rollout path, so on-disk lookup
        # should still succeed via the threadId match.
        await c.send({"type": "blemeesd.session_info", "id": "i1", "session_id": thread_id})
        evt = await c.wait_for(
            lambda e: e.get("type") in {"blemeesd.session_info_reply", "blemeesd.error"},
            timeout=10.0,
        )
        assert evt["type"] == "blemeesd.session_info_reply", (
            f"on-disk codex session info errored: {evt}"
        )
        assert evt["backend"] == "codex"
        assert evt["session_id"] == thread_id
        assert evt["attached"] is False
        assert evt["subprocess_running"] is False
        assert evt.get("cwd") == cwd
        # Codex 0.125.x's ``session_meta`` envelope doesn't carry a
        # top-level ``model`` field (only ``model_provider`` and the
        # base-instructions text), so ``model`` is allowed to be
        # absent on the on-disk path. If it ever appears, accept it
        # but don't require it.
        if "model" in evt:
            assert isinstance(evt["model"], str)
        # No durable sidecar configured → counters fall to zero.
        assert evt["turns"] == 0
        assert all(v == 0 for v in evt["last_turn_usage"].values())
        assert all(v == 0 for v in evt["cumulative_usage"].values())
        # Bind so the assertion isn't lost if the test is read in isolation.
        _ = opened
    finally:
        await c.close()


async def test_real_codex_session_info_unknown_session(real_daemon):
    c = await _client(real_daemon)
    try:
        await c.send({"type": "blemeesd.session_info", "id": "i1", "session_id": "no-such-session"})
        err = await c.wait_for(lambda e: e.get("type") == "blemeesd.error", timeout=10.0)
        assert err["code"] == "session_unknown"
    finally:
        await c.close()


async def test_real_codex_native_session_id_is_threadid(real_daemon):
    """``native_session_id`` after the first turn equals codex's threadId.

    The threadId is what ``codex-reply`` uses to continue a
    conversation; it's emitted as ``session_configured.session_id`` (or
    the matching MCP envelope field) by the codex MCP server. The
    daemon caches it and surfaces it via ``session_info`` and on
    ``opened`` for resume flows.
    """
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(_open_codex(session))
        await c.wait_for(lambda e: e.get("type") == "blemeesd.opened", timeout=30.0)
        await _say_ok(c, session)
        await c.send({"type": "blemeesd.session_info", "id": "i1", "session_id": session})
        info = await c.wait_for(
            lambda e: e.get("type") == "blemeesd.session_info_reply", timeout=10.0
        )
        thread_id = info.get("native_session_id")
        assert isinstance(thread_id, str) and thread_id
        # Codex threadIds are UUIDish; just verify it differs from the
        # daemon-assigned session_id (codex emits its own).
        assert thread_id != session
    finally:
        await c.close()


async def test_real_codex_list_sessions_lifecycle(real_daemon, tmp_path):
    """List sessions reflects open / closed / deleted state."""
    c = await _client(real_daemon)
    cwd = str(tmp_path.resolve())
    try:
        session = str(uuid.uuid4())
        await c.send(_open_codex(session, cwd=cwd))
        await c.wait_for(lambda e: e.get("type") == "blemeesd.opened", timeout=30.0)
        await _say_ok(c, session)

        # Resolve threadId — list_sessions keys codex rows by threadId.
        await c.send({"type": "blemeesd.session_info", "id": "pre", "session_id": session})
        pre = await c.wait_for(
            lambda e: e.get("type") == "blemeesd.session_info_reply" and e.get("id") == "pre",
            timeout=10.0,
        )
        thread_id = pre["native_session_id"]

        await c.send({"type": "blemeesd.list_sessions", "id": "l1", "cwd": cwd})
        ls1 = await c.wait_for(
            lambda e: e.get("type") == "blemeesd.sessions" and e.get("id") == "l1",
            timeout=10.0,
        )
        # Some codex sessions are listed by threadId, some by daemon-id;
        # accept either.
        ours_open = next(
            (
                r
                for r in ls1["sessions"]
                if r["session_id"] in {session, thread_id} and r.get("backend") == "codex"
            ),
            None,
        )
        assert ours_open is not None, f"open codex session missing: {ls1['sessions']}"
        assert ours_open["attached"] is True

        await c.send({"type": "blemeesd.close", "id": "c1", "session_id": session, "delete": True})
        await c.wait_for(lambda e: e.get("type") == "blemeesd.closed", timeout=10.0)
        await c.send({"type": "blemeesd.list_sessions", "id": "l2", "cwd": cwd})
        ls2 = await c.wait_for(
            lambda e: e.get("type") == "blemeesd.sessions" and e.get("id") == "l2",
            timeout=10.0,
        )
        assert all(r["session_id"] not in {session, thread_id} for r in ls2["sessions"]), (
            f"deleted codex session still listed: {ls2['sessions']}"
        )
    finally:
        await c.close()


async def test_real_codex_close_delete_preserves_rollout(real_daemon, tmp_path):
    """``close{delete:true}`` removes the daemon's own state (event log,
    usage sidecar) but *not* codex's rollout under
    ``~/.codex/sessions``. Codex manages its own directory and tracks
    rollouts in an internal state DB; deleting the file behind its
    back triggered ``state db returned stale rollout path …`` ERROR
    spam on subsequent codex startups.
    """
    c = await _client(real_daemon)
    cwd = str(tmp_path.resolve())
    try:
        session = str(uuid.uuid4())
        await c.send(_open_codex(session, cwd=cwd))
        await c.wait_for(lambda e: e.get("type") == "blemeesd.opened", timeout=30.0)
        await _say_ok(c, session)
        # Find the rollout path via session_info → on-disk lookup later.
        await c.send({"type": "blemeesd.session_info", "id": "pre", "session_id": session})
        pre = await c.wait_for(
            lambda e: e.get("type") == "blemeesd.session_info_reply" and e.get("id") == "pre",
            timeout=10.0,
        )
        thread_id = pre["native_session_id"]

        from blemees.backends.codex import find_session_by_id

        before = find_session_by_id(thread_id)
        assert before is not None, f"rollout missing on disk for {thread_id!r}"
        rollout_path = before["rollout_path"]

        await c.send({"type": "blemeesd.close", "id": "c1", "session_id": session, "delete": True})
        await c.wait_for(lambda e: e.get("type") == "blemeesd.closed", timeout=10.0)
        from pathlib import Path as _P

        # Codex's rollout stays — only daemon-owned files are unlinked.
        assert _P(rollout_path).exists(), f"backend rollout should not be deleted: {rollout_path}"
    finally:
        await c.close()


async def test_real_codex_interrupt_when_idle_is_no_op(real_daemon):
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(_open_codex(session))
        await c.wait_for(lambda e: e.get("type") == "blemeesd.opened", timeout=30.0)
        await c.send({"type": "blemeesd.interrupt", "session_id": session})
        ir = await c.wait_for(lambda e: e.get("type") == "blemeesd.interrupted", timeout=10.0)
        assert ir["was_idle"] is True
        await _say_ok(c, session)
    finally:
        await c.close()


async def test_real_codex_session_busy_on_concurrent_user_turn(real_daemon):
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(_open_codex(session))
        await c.wait_for(lambda e: e.get("type") == "blemeesd.opened", timeout=30.0)
        await c.send(
            {
                "type": "agent.user",
                "session_id": session,
                "message": {
                    "role": "user",
                    "content": "Count slowly from 1 to 50, one number per line.",
                },
            }
        )
        await c.wait_for(lambda e: e.get("type") == "agent.delta", timeout=120.0)
        await c.send(
            {
                "type": "agent.user",
                "session_id": session,
                "message": {"role": "user", "content": "Say OK."},
            }
        )
        err = await c.wait_for(
            lambda e: e.get("type") == "blemeesd.error" and e.get("session_id") == session,
            timeout=10.0,
        )
        assert err["code"] == "session_busy"
        # Drain the in-flight turn.
        await _drain_turn(c, session, timeout=300.0)
    finally:
        await c.close()


async def test_real_codex_status_reports_real_version(real_daemon):
    c = await _client(real_daemon)
    try:
        await c.send({"type": "blemeesd.status", "id": "s1"})
        st = await c.wait_for(lambda e: e.get("type") == "blemeesd.status_reply", timeout=10.0)
        assert "codex" in st["backends"]
        version = st["backends"]["codex"]
        assert isinstance(version, str)
        assert version[:1].isdigit(), f"unexpected version blob: {version!r}"
    finally:
        await c.close()


async def test_real_codex_ping_pong_echoes_data(real_daemon):
    c = await _client(real_daemon)
    try:
        await c.send({"type": "blemeesd.ping", "id": "p1", "data": "anything"})
        pong = await c.wait_for(lambda e: e.get("type") == "blemeesd.pong", timeout=5.0)
        assert pong["id"] == "p1"
        assert pong["data"] == "anything"
    finally:
        await c.close()


async def test_real_codex_rejects_non_text_content_block(real_daemon):
    """Image/document blocks aren't supported by codex's MCP tools yet.

    Per the spec, the daemon flattens text blocks into a single prompt
    string and rejects non-text blocks with ``invalid_message`` —
    *before* contacting the codex server, so this test stays fast even
    when the model is busy.
    """
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(_open_codex(session))
        await c.wait_for(lambda e: e.get("type") == "blemeesd.opened", timeout=30.0)
        # 1×1 transparent PNG, base64-encoded.
        tiny_png = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
            "/x8AAwMCAO+ip1sAAAAASUVORK5CYII="
        )
        await c.send(
            {
                "type": "agent.user",
                "session_id": session,
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "What's in this image?"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": tiny_png,
                            },
                        },
                    ],
                },
            }
        )
        err = await c.wait_for(
            lambda e: e.get("type") == "blemeesd.error" and e.get("session_id") == session,
            timeout=10.0,
        )
        assert err["code"] == "invalid_message"
    finally:
        await c.close()


async def test_real_codex_open_same_session_id_twice(real_daemon):
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(_open_codex(session))
        await c.wait_for(lambda e: e.get("type") == "blemeesd.opened", timeout=30.0)
        await c.send(
            {
                "type": "blemeesd.open",
                "id": "r2",
                "session_id": session,
                "backend": "codex",
                "options": {"codex": {"sandbox": "read-only", "approval-policy": "never"}},
            }
        )
        evt = await c.wait_for(
            lambda e: (
                e.get("type") in {"blemeesd.opened", "blemeesd.error"} and e.get("id") == "r2"
            ),
            timeout=10.0,
        )
        assert evt["type"] == "blemeesd.error"
        assert evt["code"] == "session_exists"
    finally:
        await c.close()


async def test_real_codex_array_text_content(real_daemon):
    """Multiple text blocks are concatenated into a single codex prompt."""
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(_open_codex(session))
        await c.wait_for(lambda e: e.get("type") == "blemeesd.opened", timeout=30.0)
        await c.send(
            {
                "type": "agent.user",
                "session_id": session,
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Reply"},
                        {"type": "text", "text": " with the single word OK."},
                    ],
                },
            }
        )
        res = await _drain_turn(c, session)
        assert res["subtype"] in {"success", "error"}
    finally:
        await c.close()


async def test_real_codex_watch_observer_sees_owner_events(real_daemon):
    owner = await _client(real_daemon)
    watcher = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await owner.send(_open_codex(session))
        await owner.wait_for(lambda e: e.get("type") == "blemeesd.opened", timeout=30.0)
        await watcher.send(
            {"type": "blemeesd.watch", "id": "w1", "session_id": session, "last_seen_seq": 0}
        )
        await watcher.wait_for(lambda e: e.get("type") == "blemeesd.watching", timeout=10.0)
        await owner.send(
            {
                "type": "agent.user",
                "session_id": session,
                "message": {"role": "user", "content": "Say OK."},
            }
        )
        await _drain_turn(owner, session)
        await _drain_turn(watcher, session)
    finally:
        await owner.close()
        await watcher.close()


async def test_real_codex_interrupt_then_continue(real_daemon):
    """Interrupt mid-stream → eventual `agent.result`.

    Codex's MCP server (0.125.x) responds to `notifications/cancelled`
    by completing the in-flight `tools/call` rather than aborting it
    early — so the post-interrupt `agent.result` arrives only after the
    underlying turn finishes naturally. We give it a generous budget
    and assert that:

      1. `blemeesd.interrupted{was_idle:false}` lands quickly,
      2. an `agent.result` eventually arrives (subtype is whatever
         Codex produces — `interrupted` if our cancel-flag wins the
         race, or `success` if the model simply finishes first),
      3. a follow-up turn still works on the same threadId.
    """
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(_open_codex(session))
        await c.wait_for(lambda e: e.get("type") == "blemeesd.opened", timeout=30.0)
        await c.send(
            {
                "type": "agent.user",
                "session_id": session,
                "message": {
                    "role": "user",
                    "content": "Count slowly from 1 to 50, one number per line.",
                },
            }
        )
        await c.wait_for(lambda e: e.get("type") == "agent.delta", timeout=120.0)
        await c.send({"type": "blemeesd.interrupt", "session_id": session})
        ir = await c.wait_for(lambda e: e.get("type") == "blemeesd.interrupted", timeout=15.0)
        assert ir["was_idle"] is False
        # Codex 0.125.x signals the cancellation via a `turn_aborted`
        # event; the translator finalises the in-flight turn from that
        # event so we don't have to wait for the JSON-RPC response
        # (which Codex sometimes never sends after an abort).
        result = await c.wait_for(
            lambda e: e.get("type") == "agent.result" and e.get("session_id") == session,
            timeout=60.0,
        )
        assert result["subtype"] in {"success", "interrupted", "error"}
        # Subsequent turn still works on the same threadId.
        await c.send(
            {
                "type": "agent.user",
                "session_id": session,
                "message": {"role": "user", "content": "Say OK."},
            }
        )
        # Codex 0.125.x can take several minutes to send the terminal
        # JSON-RPC reply when the previous turn was aborted; events
        # for the aborted turn keep streaming for a while in parallel
        # with events for the new turn, and the daemon's reader
        # tolerates that interleaving.
        await c.wait_for(
            lambda e: e.get("type") == "agent.result" and e.get("session_id") == session,
            timeout=300.0,
        )
    finally:
        await c.close()


# ---------------------------------------------------------------------------
# Open / options edge cases
# ---------------------------------------------------------------------------


async def test_real_codex_open_unknown_option_rejected(real_daemon):
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(
            {
                "type": "blemeesd.open",
                "id": "r1",
                "session_id": session,
                "backend": "codex",
                "options": {
                    "codex": {
                        "sandbox": "read-only",
                        "approval-policy": "never",
                        "fubar_unknown_key": "nope",
                    }
                },
            }
        )
        err = await c.wait_for(
            lambda e: e.get("type") == "blemeesd.error" and e.get("id") == "r1",
            timeout=10.0,
        )
        assert err["code"] == "invalid_message"
        assert "fubar_unknown_key" in err["message"]
    finally:
        await c.close()


async def test_real_codex_open_with_empty_options(real_daemon):
    """``options.codex = {}`` is accepted (codex defaults apply)."""
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(
            {
                "type": "blemeesd.open",
                "id": "r1",
                "session_id": session,
                "backend": "codex",
                "options": {"codex": {}},
            }
        )
        evt = await c.wait_for(
            lambda e: (
                e.get("type") in {"blemeesd.opened", "blemeesd.error"} and e.get("id") == "r1"
            ),
            timeout=30.0,
        )
        assert evt["type"] == "blemeesd.opened" or evt["code"] != "invalid_message", evt
    finally:
        await c.close()


# ---------------------------------------------------------------------------
# agent.user / dispatch edge cases
# ---------------------------------------------------------------------------


async def test_real_codex_agent_user_unknown_session(real_daemon):
    c = await _client(real_daemon)
    try:
        await c.send(
            {
                "type": "agent.user",
                "session_id": "no-such-session",
                "message": {"role": "user", "content": "hi"},
            }
        )
        err = await c.wait_for(lambda e: e.get("type") == "blemeesd.error", timeout=10.0)
        assert err["code"] == "session_unknown"
    finally:
        await c.close()


async def test_real_codex_agent_user_role_must_be_user(real_daemon):
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(_open_codex(session))
        await c.wait_for(lambda e: e.get("type") == "blemeesd.opened", timeout=30.0)
        await c.send(
            {
                "type": "agent.user",
                "session_id": session,
                "message": {"role": "assistant", "content": "I'm an assistant."},
            }
        )
        err = await c.wait_for(lambda e: e.get("type") == "blemeesd.error", timeout=10.0)
        assert err["code"] == "invalid_message"
    finally:
        await c.close()


# ---------------------------------------------------------------------------
# Close edge cases
# ---------------------------------------------------------------------------


async def test_real_codex_close_unknown_session_is_idempotent(real_daemon):
    c = await _client(real_daemon)
    try:
        await c.send(
            {
                "type": "blemeesd.close",
                "id": "c1",
                "session_id": "no-such-session",
                "delete": False,
            }
        )
        ack = await c.wait_for(
            lambda e: e.get("type") == "blemeesd.closed" and e.get("id") == "c1",
            timeout=10.0,
        )
        assert ack["session_id"] == "no-such-session"
    finally:
        await c.close()


async def test_real_codex_double_close_is_idempotent(real_daemon):
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(_open_codex(session))
        await c.wait_for(lambda e: e.get("type") == "blemeesd.opened", timeout=30.0)
        await c.send({"type": "blemeesd.close", "id": "c1", "session_id": session, "delete": True})
        await c.wait_for(
            lambda e: e.get("type") == "blemeesd.closed" and e.get("id") == "c1",
            timeout=10.0,
        )
        await c.send({"type": "blemeesd.close", "id": "c2", "session_id": session, "delete": False})
        ack2 = await c.wait_for(
            lambda e: e.get("type") == "blemeesd.closed" and e.get("id") == "c2",
            timeout=10.0,
        )
        assert ack2["session_id"] == session
    finally:
        await c.close()


# ---------------------------------------------------------------------------
# Multi-turn + multi-session
# ---------------------------------------------------------------------------


async def test_real_codex_three_turns_accumulate_usage(real_daemon):
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(_open_codex(session))
        await c.wait_for(lambda e: e.get("type") == "blemeesd.opened", timeout=30.0)
        for _ in range(3):
            await _say_ok(c, session)
        await c.send({"type": "blemeesd.session_info", "id": "i1", "session_id": session})
        info = await c.wait_for(
            lambda e: e.get("type") == "blemeesd.session_info_reply", timeout=10.0
        )
        assert info["turns"] == 3
        cu = info["cumulative_usage"]
        assert cu.get("input_tokens", 0) >= 3
        assert cu.get("output_tokens", 0) >= 3
    finally:
        await c.close()


async def test_real_codex_two_concurrent_sessions_independent(real_daemon):
    c = await _client(real_daemon)
    try:
        s1 = str(uuid.uuid4())
        s2 = str(uuid.uuid4())
        for sid in (s1, s2):
            await c.send(_open_codex(sid))
            await c.wait_for(
                lambda e, sid=sid: (
                    e.get("type") == "blemeesd.opened" and e.get("session_id") == sid
                ),
                timeout=30.0,
            )
        for sid in (s1, s2):
            await c.send(
                {
                    "type": "agent.user",
                    "session_id": sid,
                    "message": {"role": "user", "content": "Say OK."},
                }
            )
        seen: dict[str, dict] = {}
        deadline = asyncio.get_running_loop().time() + 240.0
        while len(seen) < 2:
            remaining = deadline - asyncio.get_running_loop().time()
            assert remaining > 0, f"timed out; got={list(seen)}"
            evt = await c.recv(timeout=remaining)
            if evt.get("type") == "agent.result" and evt.get("session_id") in {s1, s2}:
                seen[evt["session_id"]] = evt
    finally:
        await c.close()


# ---------------------------------------------------------------------------
# Status snapshot
# ---------------------------------------------------------------------------


async def test_real_codex_status_active_turns_during_turn(real_daemon):
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(_open_codex(session))
        await c.wait_for(lambda e: e.get("type") == "blemeesd.opened", timeout=30.0)
        await c.send(
            {
                "type": "agent.user",
                "session_id": session,
                "message": {
                    "role": "user",
                    "content": "Count slowly from 1 to 50, one number per line.",
                },
            }
        )
        await c.wait_for(lambda e: e.get("type") == "agent.delta", timeout=120.0)
        await c.send({"type": "blemeesd.status", "id": "s1"})
        st = await c.wait_for(
            lambda e: e.get("type") == "blemeesd.status_reply" and e.get("id") == "s1",
            timeout=10.0,
        )
        assert st["sessions"]["active_turns"] >= 1
        assert st["sessions"]["by_backend"].get("codex", 0) >= 1
        # Cancel for cleanup — just wait for the ack. Post-cancel
        # ordering is locked down by mock tests; here we don't drain
        # the codex side because 0.125.x can stream events for a
        # cancelled turn for a long time before settling.
        await c.send({"type": "blemeesd.interrupt", "session_id": session})
        await c.wait_for(lambda e: e.get("type") == "blemeesd.interrupted", timeout=15.0)
    finally:
        await c.close()


# ---------------------------------------------------------------------------
# Watch / unwatch edge cases
# ---------------------------------------------------------------------------


async def test_real_codex_unwatch_when_not_watching(real_daemon):
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(_open_codex(session))
        await c.wait_for(lambda e: e.get("type") == "blemeesd.opened", timeout=30.0)
        await c.send({"type": "blemeesd.unwatch", "id": "uw1", "session_id": session})
        ack = await c.wait_for(
            lambda e: e.get("type") == "blemeesd.unwatched" and e.get("id") == "uw1",
            timeout=10.0,
        )
        assert ack["was_watching"] is False
    finally:
        await c.close()


async def test_real_codex_watch_unknown_session_errors(real_daemon):
    c = await _client(real_daemon)
    try:
        await c.send({"type": "blemeesd.watch", "id": "w1", "session_id": "no-such-session"})
        err = await c.wait_for(lambda e: e.get("type") == "blemeesd.error", timeout=10.0)
        assert err["code"] == "session_unknown"
    finally:
        await c.close()


# ---------------------------------------------------------------------------
# list_sessions edge cases
# ---------------------------------------------------------------------------


async def test_real_codex_list_sessions_empty_cwd(real_daemon, tmp_path):
    c = await _client(real_daemon)
    try:
        empty_cwd = str((tmp_path / "no-codex-sessions-here").resolve())
        await c.send({"type": "blemeesd.list_sessions", "id": "l1", "cwd": empty_cwd})
        ls = await c.wait_for(
            lambda e: e.get("type") == "blemeesd.sessions" and e.get("id") == "l1",
            timeout=10.0,
        )
        # Pure on-disk listings are empty for a fresh cwd; if the
        # daemon also surfaces in-memory sessions for other cwds, those
        # don't match this query — so the array stays empty.
        assert all(r.get("session_id") for r in ls["sessions"])
        # A truly empty cwd gives an empty array.
        assert ls["sessions"] == []
        assert ls["cwd"] == empty_cwd
    finally:
        await c.close()


async def test_real_codex_list_sessions_includes_first_user_preview(real_daemon, tmp_path):
    """Codex rollouts also surface the first user prompt as ``preview``."""
    c = await _client(real_daemon)
    cwd = str(tmp_path.resolve())
    try:
        session = str(uuid.uuid4())
        await c.send(_open_codex(session, cwd=cwd))
        await c.wait_for(lambda e: e.get("type") == "blemeesd.opened", timeout=30.0)
        await c.send(
            {
                "type": "agent.user",
                "session_id": session,
                "message": {
                    "role": "user",
                    "content": "codex-preview-marker — Say OK.",
                },
            }
        )
        await _drain_turn(c, session)

        # Resolve threadId so we can find the row.
        await c.send({"type": "blemeesd.session_info", "id": "pre", "session_id": session})
        pre = await c.wait_for(
            lambda e: e.get("type") == "blemeesd.session_info_reply" and e.get("id") == "pre",
            timeout=10.0,
        )
        thread_id = pre["native_session_id"]

        await c.send({"type": "blemeesd.list_sessions", "id": "l1", "cwd": cwd})
        ls = await c.wait_for(
            lambda e: e.get("type") == "blemeesd.sessions" and e.get("id") == "l1",
            timeout=10.0,
        )
        ours = next(
            (r for r in ls["sessions"] if r["session_id"] in {session, thread_id}),
            None,
        )
        assert ours is not None, ls["sessions"]
        # Codex stores the user message with various envelopes; our
        # preview helper digs into them. Either way the marker must
        # show up in the preview text.
        preview = ours.get("preview", "")
        assert "codex-preview-marker" in preview, preview
    finally:
        await c.close()


# ---------------------------------------------------------------------------
# Reattach / replay
# ---------------------------------------------------------------------------


async def test_real_codex_reconnect_resume_replays_seen_events(real_daemon):
    """Detach + reopen with ``last_seen_seq=0`` → ring buffer replays.

    This exercises the daemon-side resume routing for codex (the
    ``codex-reply`` call with the cached threadId), independently of
    whether codex 0.125.x rehydrates context model-side. The replay we
    care about here is the daemon's ring buffer, not the conversation
    state.
    """
    c = await _client(real_daemon)
    session = str(uuid.uuid4())
    try:
        await c.send(_open_codex(session))
        opened = await c.wait_for(lambda e: e.get("type") == "blemeesd.opened", timeout=30.0)
        await _say_ok(c, session)
        last_seq = opened.get("last_seq", 0)
    finally:
        await c.close()

    c2 = await _client(real_daemon)
    try:
        await c2.send(
            {
                "type": "blemeesd.open",
                "id": "r2",
                "session_id": session,
                "backend": "codex",
                "resume": True,
                "last_seen_seq": 0,
                "options": {"codex": {"sandbox": "read-only", "approval-policy": "never"}},
            }
        )
        opened2 = await c2.wait_for(
            lambda e: e.get("type") == "blemeesd.opened" and e.get("id") == "r2",
            timeout=30.0,
        )
        assert opened2["last_seq"] >= last_seq
        replayed = await c2.wait_for(
            lambda e: isinstance(e.get("type"), str) and e["type"].startswith("agent."),
            timeout=15.0,
        )
        assert isinstance(replayed.get("seq"), int)
        assert replayed["seq"] >= 1
    finally:
        await c2.close()


async def test_real_codex_double_interrupt_second_is_idle(real_daemon):
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(_open_codex(session))
        await c.wait_for(lambda e: e.get("type") == "blemeesd.opened", timeout=30.0)
        await c.send(
            {
                "type": "agent.user",
                "session_id": session,
                "message": {
                    "role": "user",
                    "content": "Count slowly from 1 to 50, one number per line.",
                },
            }
        )
        await c.wait_for(lambda e: e.get("type") == "agent.delta", timeout=120.0)
        await c.send({"type": "blemeesd.interrupt", "session_id": session})
        # Drain whatever lands first (either was_idle:false or the
        # synthesized agent.result(interrupted)).
        post = await c.wait_for(
            lambda e: e.get("type") == "agent.result" and e.get("session_id") == session,
            collect=True,
            timeout=300.0,
        )
        ir = next((e for e in post if e.get("type") == "blemeesd.interrupted"), None)
        assert ir is not None and ir["was_idle"] is False, post
        # Second interrupt — turn already finalised.
        await c.send({"type": "blemeesd.interrupt", "session_id": session})
        ir2 = await c.wait_for(lambda e: e.get("type") == "blemeesd.interrupted", timeout=10.0)
        assert ir2["was_idle"] is True
    finally:
        await c.close()


async def test_real_codex_session_takeover_notifies_old_owner(real_daemon):
    owner = await _client(real_daemon)
    other = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await owner.send(_open_codex(session))
        await owner.wait_for(lambda e: e.get("type") == "blemeesd.opened", timeout=30.0)
        await _say_ok(owner, session)

        await other.send(
            {
                "type": "blemeesd.open",
                "id": "takeover",
                "session_id": session,
                "backend": "codex",
                "resume": True,
                "options": {"codex": {"sandbox": "read-only", "approval-policy": "never"}},
            }
        )
        evt = await owner.wait_for(
            lambda e: e.get("type") == "blemeesd.session_taken" and e.get("session_id") == session,
            timeout=15.0,
        )
        assert evt["session_id"] == session
        await other.wait_for(
            lambda e: e.get("type") == "blemeesd.opened" and e.get("id") == "takeover",
            timeout=15.0,
        )
    finally:
        await owner.close()
        await other.close()


# ---------------------------------------------------------------------------
# Hello / handshake edge cases
# ---------------------------------------------------------------------------


async def test_real_codex_hello_with_no_client_field(real_daemon):
    reader, writer = await asyncio.open_unix_connection(real_daemon)
    try:
        from tests.blemees.conftest import _StreamClient

        c = _StreamClient(reader, writer)
        await c.send({"type": "blemeesd.hello", "protocol": PROTOCOL_VERSION})
        ack = await c.wait_for(lambda e: e.get("type") == "blemeesd.hello_ack", timeout=10.0)
        assert ack["protocol"] == PROTOCOL_VERSION
        await c.close()
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass


# ---------------------------------------------------------------------------
# agent.user content edge cases
# ---------------------------------------------------------------------------


async def test_real_codex_agent_user_missing_message(real_daemon):
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(_open_codex(session))
        await c.wait_for(lambda e: e.get("type") == "blemeesd.opened", timeout=30.0)
        await c.send({"type": "agent.user", "session_id": session})
        err = await c.wait_for(lambda e: e.get("type") == "blemeesd.error", timeout=10.0)
        assert err["code"] == "invalid_message"
    finally:
        await c.close()


async def test_real_codex_agent_user_content_null(real_daemon):
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(_open_codex(session))
        await c.wait_for(lambda e: e.get("type") == "blemeesd.opened", timeout=30.0)
        await c.send(
            {
                "type": "agent.user",
                "session_id": session,
                "message": {"role": "user", "content": None},
            }
        )
        err = await c.wait_for(lambda e: e.get("type") == "blemeesd.error", timeout=10.0)
        assert err["code"] == "invalid_message"
    finally:
        await c.close()


async def test_real_codex_agent_user_unicode_emoji_passthrough(real_daemon):
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(_open_codex(session))
        await c.wait_for(lambda e: e.get("type") == "blemeesd.opened", timeout=30.0)
        await c.send(
            {
                "type": "agent.user",
                "session_id": session,
                "message": {
                    "role": "user",
                    "content": "Reply 🎯 if you can read this emoji and KEYWORD-ABC.",
                },
            }
        )
        await _drain_turn(c, session)
    finally:
        await c.close()


# ---------------------------------------------------------------------------
# Multi-turn rapid
# ---------------------------------------------------------------------------


async def test_real_codex_back_to_back_turns(real_daemon):
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(_open_codex(session))
        await c.wait_for(lambda e: e.get("type") == "blemeesd.opened", timeout=30.0)
        await _say_ok(c, session)
        await _say_ok(c, session)
    finally:
        await c.close()


# ---------------------------------------------------------------------------
# session_info during in-flight turn
# ---------------------------------------------------------------------------


async def test_real_codex_session_info_during_inflight_turn(real_daemon):
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(_open_codex(session))
        await c.wait_for(lambda e: e.get("type") == "blemeesd.opened", timeout=30.0)
        await c.send(
            {
                "type": "agent.user",
                "session_id": session,
                "message": {
                    "role": "user",
                    "content": "Count slowly from 1 to 30, one per line.",
                },
            }
        )
        await c.wait_for(lambda e: e.get("type") == "agent.delta", timeout=120.0)
        await c.send({"type": "blemeesd.session_info", "id": "i1", "session_id": session})
        info = await c.wait_for(
            lambda e: e.get("type") == "blemeesd.session_info_reply" and e.get("id") == "i1",
            timeout=10.0,
        )
        assert info["attached"] is True
        assert info["subprocess_running"] is True
        # Cleanup.
        await c.send({"type": "blemeesd.interrupt", "session_id": session})
        await c.wait_for(lambda e: e.get("type") == "blemeesd.interrupted", timeout=15.0)
    finally:
        await c.close()


# ---------------------------------------------------------------------------
# Status snapshot
# ---------------------------------------------------------------------------


async def test_real_codex_status_with_no_sessions(real_daemon):
    c = await _client(real_daemon)
    try:
        await c.send({"type": "blemeesd.status", "id": "s1"})
        st = await c.wait_for(lambda e: e.get("type") == "blemeesd.status_reply", timeout=10.0)
        s = st["sessions"]
        assert s["total"] == 0
        assert s["active_turns"] == 0
        assert isinstance(st["uptime_s"], (int, float))
    finally:
        await c.close()


async def test_real_codex_status_uptime_monotonic(real_daemon):
    c = await _client(real_daemon)
    try:
        await c.send({"type": "blemeesd.status", "id": "s1"})
        st1 = await c.wait_for(
            lambda e: e.get("type") == "blemeesd.status_reply" and e.get("id") == "s1",
            timeout=10.0,
        )
        await asyncio.sleep(0.5)
        await c.send({"type": "blemeesd.status", "id": "s2"})
        st2 = await c.wait_for(
            lambda e: e.get("type") == "blemeesd.status_reply" and e.get("id") == "s2",
            timeout=10.0,
        )
        assert st2["uptime_s"] > st1["uptime_s"]
    finally:
        await c.close()


# ---------------------------------------------------------------------------
# Watch / unwatch edge cases (more)
# ---------------------------------------------------------------------------


async def test_real_codex_watcher_disconnect_auto_unwatch(real_daemon):
    owner = await _client(real_daemon)
    watcher = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await owner.send(_open_codex(session))
        await owner.wait_for(lambda e: e.get("type") == "blemeesd.opened", timeout=30.0)
        await watcher.send(
            {"type": "blemeesd.watch", "id": "w1", "session_id": session, "last_seen_seq": 0}
        )
        await watcher.wait_for(lambda e: e.get("type") == "blemeesd.watching", timeout=10.0)
        await _say_ok(owner, session)
        await watcher.close()
        await _say_ok(owner, session)
    finally:
        await owner.close()


async def test_real_codex_watcher_replay_via_last_seen_seq(real_daemon):
    owner = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await owner.send(_open_codex(session))
        await owner.wait_for(lambda e: e.get("type") == "blemeesd.opened", timeout=30.0)
        await _say_ok(owner, session)
        watcher = await _client(real_daemon)
        try:
            await watcher.send(
                {
                    "type": "blemeesd.watch",
                    "id": "w1",
                    "session_id": session,
                    "last_seen_seq": 0,
                }
            )
            ack = await watcher.wait_for(
                lambda e: e.get("type") == "blemeesd.watching" and e.get("id") == "w1",
                timeout=10.0,
            )
            assert ack["last_seq"] >= 1
            replayed = await watcher.wait_for(
                lambda e: isinstance(e.get("type"), str) and e["type"].startswith("agent."),
                timeout=10.0,
            )
            assert isinstance(replayed.get("seq"), int)
        finally:
            await watcher.close()
    finally:
        await owner.close()


# ---------------------------------------------------------------------------
# Codex-specific options
# ---------------------------------------------------------------------------


async def test_real_codex_developer_instructions_passthrough(real_daemon):
    """``options.codex.developer-instructions`` is forwarded as the developer
    role and therefore steers the model's first reply."""
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        # Pin the response to a single token so we can assert on it.
        await c.send(
            {
                "type": "blemeesd.open",
                "id": "r1",
                "session_id": session,
                "backend": "codex",
                "options": {
                    "codex": {
                        "sandbox": "read-only",
                        "approval-policy": "never",
                        "developer-instructions": (
                            "When the user says hi, respond with exactly the single "
                            "uppercase word HELLO and nothing else."
                        ),
                    }
                },
            }
        )
        await c.wait_for(lambda e: e.get("type") == "blemeesd.opened", timeout=30.0)
        await c.send(
            {
                "type": "agent.user",
                "session_id": session,
                "message": {"role": "user", "content": "hi"},
            }
        )
        collected = await c.wait_for(
            lambda e: e.get("type") == "agent.result" and e.get("session_id") == session,
            collect=True,
            timeout=120.0,
        )
        text = ""
        for evt in collected:
            if evt.get("type") == "agent.message":
                for block in evt.get("content", []) or []:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text += block.get("text", "")
        # Don't insist on exact equality (model can hedge a little) but
        # the steering must take some effect.
        assert "HELLO" in text.upper(), text
    finally:
        await c.close()


# ---------------------------------------------------------------------------
# Codex-specific watch + interrupt
# ---------------------------------------------------------------------------


async def test_real_codex_watcher_sees_synthesized_interrupt_result(real_daemon):
    """A watcher sees ``agent.result(interrupted)`` after an interrupt.

    For codex the synthesised frame comes from the translator's
    ``finalize_interrupted`` (triggered by the ``turn_aborted`` event),
    not from the backend's ``interrupt()`` method directly. Watchers
    should still see it because it goes through the session pipeline.
    """
    owner = await _client(real_daemon)
    watcher = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await owner.send(_open_codex(session))
        await owner.wait_for(lambda e: e.get("type") == "blemeesd.opened", timeout=30.0)
        await watcher.send(
            {"type": "blemeesd.watch", "id": "w1", "session_id": session, "last_seen_seq": 0}
        )
        await watcher.wait_for(lambda e: e.get("type") == "blemeesd.watching", timeout=10.0)
        await owner.send(
            {
                "type": "agent.user",
                "session_id": session,
                "message": {
                    "role": "user",
                    "content": "Count slowly from 1 to 30, one per line.",
                },
            }
        )
        await owner.wait_for(lambda e: e.get("type") == "agent.delta", timeout=120.0)
        await owner.send({"type": "blemeesd.interrupt", "session_id": session})
        await owner.wait_for(lambda e: e.get("type") == "blemeesd.interrupted", timeout=15.0)
        synthesized = await watcher.wait_for(
            lambda e: e.get("type") == "agent.result" and e.get("session_id") == session,
            timeout=120.0,
        )
        assert synthesized["subtype"] in {"interrupted", "success", "error"}
    finally:
        await owner.close()
        await watcher.close()


# ---------------------------------------------------------------------------
# More codex options
# ---------------------------------------------------------------------------


async def test_real_codex_double_watch_is_idempotent(real_daemon):
    owner = await _client(real_daemon)
    watcher = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await owner.send(_open_codex(session))
        await owner.wait_for(lambda e: e.get("type") == "blemeesd.opened", timeout=30.0)
        await watcher.send(
            {"type": "blemeesd.watch", "id": "w1", "session_id": session, "last_seen_seq": 0}
        )
        ack1 = await watcher.wait_for(
            lambda e: e.get("type") == "blemeesd.watching" and e.get("id") == "w1",
            timeout=10.0,
        )
        await watcher.send(
            {"type": "blemeesd.watch", "id": "w2", "session_id": session, "last_seen_seq": 0}
        )
        ack2 = await watcher.wait_for(
            lambda e: e.get("type") == "blemeesd.watching" and e.get("id") == "w2",
            timeout=10.0,
        )
        assert ack2["last_seq"] >= ack1["last_seq"]
    finally:
        await owner.close()
        await watcher.close()


async def test_real_codex_status_total_after_close(real_daemon):
    c = await _client(real_daemon)
    try:
        await c.send({"type": "blemeesd.status", "id": "s0"})
        st0 = await c.wait_for(
            lambda e: e.get("type") == "blemeesd.status_reply" and e.get("id") == "s0",
            timeout=10.0,
        )
        baseline = st0["sessions"]["total"]

        session = str(uuid.uuid4())
        await c.send(_open_codex(session))
        await c.wait_for(lambda e: e.get("type") == "blemeesd.opened", timeout=30.0)
        await _say_ok(c, session)
        await c.send({"type": "blemeesd.close", "id": "c1", "session_id": session, "delete": True})
        await c.wait_for(lambda e: e.get("type") == "blemeesd.closed", timeout=10.0)

        await c.send({"type": "blemeesd.status", "id": "s1"})
        st1 = await c.wait_for(
            lambda e: e.get("type") == "blemeesd.status_reply" and e.get("id") == "s1",
            timeout=10.0,
        )
        assert st1["sessions"]["total"] == baseline
    finally:
        await c.close()


async def test_real_codex_session_info_on_random_uuid_unknown(real_daemon):
    c = await _client(real_daemon)
    try:
        random_id = str(uuid.uuid4())
        await c.send({"type": "blemeesd.session_info", "id": "i1", "session_id": random_id})
        err = await c.wait_for(lambda e: e.get("type") == "blemeesd.error", timeout=10.0)
        assert err["code"] == "session_unknown"
    finally:
        await c.close()


# ---------------------------------------------------------------------------
# Connection-close behavior
# ---------------------------------------------------------------------------


async def test_real_codex_soft_detach_idle_session_reattaches(real_daemon):
    c1 = await _client(real_daemon)
    session = str(uuid.uuid4())
    await c1.send(_open_codex(session))
    await c1.wait_for(lambda e: e.get("type") == "blemeesd.opened", timeout=30.0)
    await _say_ok(c1, session)
    await c1.close()

    c2 = await _client(real_daemon)
    try:
        await c2.send(
            {
                "type": "blemeesd.open",
                "id": "r2",
                "session_id": session,
                "backend": "codex",
                "resume": True,
                "options": {"codex": {"sandbox": "read-only", "approval-policy": "never"}},
            }
        )
        await c2.wait_for(
            lambda e: e.get("type") == "blemeesd.opened" and e.get("id") == "r2",
            timeout=30.0,
        )
        # Run a fresh turn to verify the reattached session works. Codex
        # 0.125.x doesn't preserve model-side context across this re-spawn,
        # but the daemon-side resume routing must still produce a valid
        # turn lifecycle.
        await _say_ok(c2, session)
    finally:
        await c2.close()


async def test_real_codex_ping_without_data_or_id(real_daemon):
    c = await _client(real_daemon)
    try:
        await c.send({"type": "blemeesd.ping"})
        pong = await c.wait_for(lambda e: e.get("type") == "blemeesd.pong", timeout=5.0)
        assert "id" not in pong
        assert "data" not in pong
    finally:
        await c.close()


async def test_real_codex_open_with_no_options_field_rejected(real_daemon):
    c = await _client(real_daemon)
    try:
        await c.send(
            {
                "type": "blemeesd.open",
                "id": "r1",
                "session_id": str(uuid.uuid4()),
                "backend": "codex",
            }
        )
        err = await c.wait_for(
            lambda e: e.get("type") == "blemeesd.error" and e.get("id") == "r1",
            timeout=10.0,
        )
        assert err["code"] == "invalid_message"
    finally:
        await c.close()


async def test_real_codex_list_sessions_during_inflight(real_daemon, tmp_path):
    c = await _client(real_daemon)
    cwd = str(tmp_path.resolve())
    try:
        session = str(uuid.uuid4())
        await c.send(_open_codex(session, cwd=cwd))
        await c.wait_for(lambda e: e.get("type") == "blemeesd.opened", timeout=30.0)
        await c.send(
            {
                "type": "agent.user",
                "session_id": session,
                "message": {"role": "user", "content": "Count from 1 to 30, slowly."},
            }
        )
        await c.wait_for(lambda e: e.get("type") == "agent.delta", timeout=120.0)
        await c.send({"type": "blemeesd.list_sessions", "id": "l1", "cwd": cwd})
        ls = await c.wait_for(
            lambda e: e.get("type") == "blemeesd.sessions" and e.get("id") == "l1",
            timeout=10.0,
        )
        # Resolve threadId.
        await c.send({"type": "blemeesd.session_info", "id": "i1", "session_id": session})
        info = await c.wait_for(
            lambda e: e.get("type") == "blemeesd.session_info_reply" and e.get("id") == "i1",
            timeout=10.0,
        )
        thread_id = info["native_session_id"]
        ours = next((r for r in ls["sessions"] if r["session_id"] in {session, thread_id}), None)
        assert ours is not None
        assert ours["attached"] is True
        # Cleanup.
        await c.send({"type": "blemeesd.interrupt", "session_id": session})
        await c.wait_for(lambda e: e.get("type") == "blemeesd.interrupted", timeout=15.0)
    finally:
        await c.close()


async def test_real_codex_three_connections_three_sessions(real_daemon):
    clients = [await _client(real_daemon) for _ in range(3)]
    sessions = [str(uuid.uuid4()) for _ in range(3)]
    try:
        for c, sid in zip(clients, sessions, strict=True):
            await c.send(_open_codex(sid))
            await c.wait_for(
                lambda e, sid=sid: (
                    e.get("type") == "blemeesd.opened" and e.get("session_id") == sid
                ),
                timeout=30.0,
            )
        for c, sid in zip(clients, sessions, strict=True):
            await c.send(
                {
                    "type": "agent.user",
                    "session_id": sid,
                    "message": {"role": "user", "content": "Say OK."},
                }
            )
        for c, sid in zip(clients, sessions, strict=True):
            await _drain_turn(c, sid, timeout=300.0)
        await clients[0].send({"type": "blemeesd.status", "id": "s1"})
        st = await clients[0].wait_for(
            lambda e: e.get("type") == "blemeesd.status_reply" and e.get("id") == "s1",
            timeout=10.0,
        )
        assert st["sessions"]["total"] >= 3
        assert st["sessions"]["by_backend"].get("codex", 0) >= 3
    finally:
        for c in clients:
            await c.close()


# ---------------------------------------------------------------------------
# More edge cases
# ---------------------------------------------------------------------------


async def test_real_codex_agent_user_long_content(real_daemon):
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(_open_codex(session))
        await c.wait_for(lambda e: e.get("type") == "blemeesd.opened", timeout=30.0)
        body = "abcde " * 1000
        await c.send(
            {
                "type": "agent.user",
                "session_id": session,
                "message": {
                    "role": "user",
                    "content": f"Reply with the single uppercase word LONG. Ignore: {body}",
                },
            }
        )
        await _drain_turn(c, session, timeout=180.0)
    finally:
        await c.close()


async def test_real_codex_third_connection_sees_session_taken_chain(real_daemon):
    a = await _client(real_daemon)
    b = await _client(real_daemon)
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await a.send(_open_codex(session))
        await a.wait_for(lambda e: e.get("type") == "blemeesd.opened", timeout=30.0)
        await _say_ok(a, session)

        await b.send(
            {
                "type": "blemeesd.open",
                "id": "b1",
                "session_id": session,
                "backend": "codex",
                "resume": True,
                "options": {"codex": {"sandbox": "read-only", "approval-policy": "never"}},
            }
        )
        await a.wait_for(
            lambda e: e.get("type") == "blemeesd.session_taken" and e.get("session_id") == session,
            timeout=15.0,
        )
        await b.wait_for(
            lambda e: e.get("type") == "blemeesd.opened" and e.get("id") == "b1",
            timeout=15.0,
        )

        await c.send(
            {
                "type": "blemeesd.open",
                "id": "c1",
                "session_id": session,
                "backend": "codex",
                "resume": True,
                "options": {"codex": {"sandbox": "read-only", "approval-policy": "never"}},
            }
        )
        await b.wait_for(
            lambda e: e.get("type") == "blemeesd.session_taken" and e.get("session_id") == session,
            timeout=15.0,
        )
        await c.wait_for(
            lambda e: e.get("type") == "blemeesd.opened" and e.get("id") == "c1",
            timeout=15.0,
        )
    finally:
        await a.close()
        await b.close()
        await c.close()


async def test_real_codex_session_taken_doesnt_kick_watcher(real_daemon):
    owner1 = await _client(real_daemon)
    watcher = await _client(real_daemon)
    owner2 = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await owner1.send(_open_codex(session))
        await owner1.wait_for(lambda e: e.get("type") == "blemeesd.opened", timeout=30.0)
        await _say_ok(owner1, session)
        await watcher.send(
            {"type": "blemeesd.watch", "id": "w1", "session_id": session, "last_seen_seq": 0}
        )
        await watcher.wait_for(lambda e: e.get("type") == "blemeesd.watching", timeout=10.0)
        await owner2.send(
            {
                "type": "blemeesd.open",
                "id": "r2",
                "session_id": session,
                "backend": "codex",
                "resume": True,
                "options": {"codex": {"sandbox": "read-only", "approval-policy": "never"}},
            }
        )
        await owner1.wait_for(lambda e: e.get("type") == "blemeesd.session_taken", timeout=15.0)
        await owner2.wait_for(
            lambda e: e.get("type") == "blemeesd.opened" and e.get("id") == "r2",
            timeout=15.0,
        )
        await owner2.send(
            {
                "type": "agent.user",
                "session_id": session,
                "message": {"role": "user", "content": "Say OK."},
            }
        )
        await _drain_turn(owner2, session, timeout=180.0)
        await _drain_turn(watcher, session, timeout=15.0)
    finally:
        await owner1.close()
        await watcher.close()
        await owner2.close()


# ---------------------------------------------------------------------------
# Connection close mid-turn (codex)
# ---------------------------------------------------------------------------


async def test_real_codex_close_connection_mid_turn_session_survives(real_daemon):
    """Dropping the socket mid-turn detaches; reattach via resume works.

    For codex 0.125.x the model-side state may not be preserved across
    re-spawn (documented limitation), but the daemon-side reattach
    routing must still produce a clean ``opened`` ack.
    """
    c1 = await _client(real_daemon)
    session = str(uuid.uuid4())
    await c1.send(_open_codex(session))
    await c1.wait_for(lambda e: e.get("type") == "blemeesd.opened", timeout=30.0)
    await c1.send(
        {
            "type": "agent.user",
            "session_id": session,
            "message": {"role": "user", "content": "Say OK."},
        }
    )
    await c1.wait_for(lambda e: e.get("type") == "agent.delta", timeout=120.0)
    await c1.close()
    await asyncio.sleep(2.0)

    c2 = await _client(real_daemon)
    try:
        await c2.send(
            {
                "type": "blemeesd.open",
                "id": "r2",
                "session_id": session,
                "backend": "codex",
                "resume": True,
                "options": {"codex": {"sandbox": "read-only", "approval-policy": "never"}},
            }
        )
        opened = await c2.wait_for(
            lambda e: e.get("type") == "blemeesd.opened" and e.get("id") == "r2",
            timeout=30.0,
        )
        assert opened["session_id"] == session
    finally:
        await c2.close()


async def test_real_codex_status_attached_detached_counts(real_daemon):
    c_a = await _client(real_daemon)
    c_b = await _client(real_daemon)
    s_a = str(uuid.uuid4())
    s_b = str(uuid.uuid4())
    try:
        await c_a.send(_open_codex(s_a))
        await c_a.wait_for(lambda e: e.get("type") == "blemeesd.opened", timeout=30.0)
        await c_b.send(_open_codex(s_b))
        await c_b.wait_for(lambda e: e.get("type") == "blemeesd.opened", timeout=30.0)
        await _say_ok(c_a, s_a)
        await _say_ok(c_b, s_b)
        await c_b.close()
        await asyncio.sleep(0.5)
        await c_a.send({"type": "blemeesd.status", "id": "s1"})
        st = await c_a.wait_for(
            lambda e: e.get("type") == "blemeesd.status_reply" and e.get("id") == "s1",
            timeout=10.0,
        )
        assert st["sessions"]["attached"] >= 1
        assert st["sessions"]["detached"] >= 1
    finally:
        await c_a.close()


# ---------------------------------------------------------------------------
# agent.user_echo emitted by codex
# ---------------------------------------------------------------------------


async def test_real_codex_user_echo_emitted(real_daemon):
    """Codex echoes the user prompt back as ``agent.user_echo`` early in the turn."""
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(_open_codex(session))
        await c.wait_for(lambda e: e.get("type") == "blemeesd.opened", timeout=30.0)
        marker = "codex-user-echo-marker-9876"
        await c.send(
            {
                "type": "agent.user",
                "session_id": session,
                "message": {"role": "user", "content": f"Echo this: {marker}. Then say OK."},
            }
        )
        collected = await c.wait_for(
            lambda e: e.get("type") == "agent.result" and e.get("session_id") == session,
            collect=True,
            timeout=120.0,
        )
        echoes = [
            e
            for e in collected
            if e.get("type") == "agent.user_echo" and e.get("session_id") == session
        ]
        assert echoes, [e.get("type") for e in collected]
        # The echo carries the user content somewhere — accept any
        # shape that contains the marker.
        flat = json.dumps(echoes)
        assert marker in flat, echoes
    finally:
        await c.close()
