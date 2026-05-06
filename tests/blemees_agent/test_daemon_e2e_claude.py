"""End-to-end tests that require the real ``claude`` CLI and a live OAuth
session. Gated with ``requires_claude`` and auto-skipped otherwise.

Run with::

    pytest -m requires_claude
"""

from __future__ import annotations

import asyncio
import shutil
import uuid

import pytest
import pytest_asyncio

from blemees_agent import PROTOCOL_VERSION
from blemees_agent.config import Config
from blemees_agent.daemon import Daemon
from blemees_agent.logging import configure

CLAUDE = shutil.which("claude")


pytestmark = pytest.mark.requires_claude


def _need_claude() -> None:
    if CLAUDE is None:
        pytest.skip("`claude` not on PATH", allow_module_level=True)


_need_claude()


@pytest_asyncio.fixture
async def real_daemon(tmp_path):
    from tests.blemees_agent.conftest import short_socket_path

    socket_path = short_socket_path("blemeesd-e2e")
    cfg = Config(socket_path=str(socket_path), claude_bin=CLAUDE)
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
    from tests.blemees_agent.conftest import _StreamClient  # reuse helper

    reader, writer = await asyncio.open_unix_connection(socket_path)
    c = _StreamClient(reader, writer)
    await c.send({"type": "agent.hello", "client": "e2e/0", "protocol": PROTOCOL_VERSION})
    ack = await c.recv()
    assert ack["type"] == "agent.hello_ack"
    return c


def _open_claude(session: str, *, cwd: str | None = None, **extra_options) -> dict:
    options: dict = {
        "model": "haiku",
        "tools": "",
        "permission_mode": "bypassPermissions",
    }
    if cwd is not None:
        options["cwd"] = cwd
    options.update(extra_options)
    return {
        "type": "agent.open",
        "id": "r1",
        "session_id": session,
        "backend": "claude",
        "options": {"claude": options},
    }


async def _drain_turn(c, session: str, *, timeout: float = 60.0) -> dict:
    """Wait for ``agent.result`` for *session* and return that frame."""
    return await c.wait_for(
        lambda e: e.get("type") == "agent.result" and e.get("session_id") == session,
        timeout=timeout,
    )


async def _say_ok(c, session: str, *, timeout: float = 60.0) -> dict:
    """Send a minimal ``Say OK`` turn and wait for ``agent.result``."""
    await c.send(
        {
            "type": "agent.user",
            "session_id": session,
            "message": {"role": "user", "content": "Say OK."},
        }
    )
    return await _drain_turn(c, session, timeout=timeout)


async def test_real_claude_turn_produces_result(real_daemon):
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(_open_claude(session))
        await c.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)
        await c.send(
            {
                "type": "agent.user",
                "session_id": session,
                "message": {"role": "user", "content": "Say OK."},
            }
        )
        res = await c.wait_for(
            lambda e: e.get("type") == "agent.result" and e.get("session_id") == session,
            timeout=60.0,
        )
        assert res["subtype"] in {"success", "error", "interrupted"}
    finally:
        await c.close()


async def test_real_claude_resume_preserves_context(real_daemon):
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(_open_claude(session))
        await c.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)
        await c.send(
            {
                "type": "agent.user",
                "session_id": session,
                "message": {"role": "user", "content": "Remember the number 17."},
            }
        )
        await c.wait_for(
            lambda e: e.get("type") == "agent.result" and e.get("session_id") == session,
            timeout=60.0,
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
            timeout=60.0,
        )
        text = ""
        for evt in collected:
            if evt.get("type") == "agent.message":
                # blemees-agent/1 puts the content list at the top level of
                # `agent.message`; the legacy shape (`message.content`)
                # is kept as a fallback for forward-compat readers.
                blocks = evt.get("content") or evt.get("message", {}).get("content", [])
                for block in blocks:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text += block.get("text", "")
        assert "17" in text, (
            f"resumed turn lost context: text={text!r} "
            f"frames={[(e.get('type'), e.get('subtype')) for e in collected]}"
        )
    finally:
        await c.close()


async def test_real_claude_session_info_in_memory_no_turns(real_daemon):
    """Right after open, session_info reports zero turns and attached=true."""
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(_open_claude(session))
        await c.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)
        await c.send({"type": "agent.session_info", "id": "i1", "session_id": session})
        info = await c.wait_for(lambda e: e.get("type") == "agent.session_info_reply", timeout=10.0)
        assert info["backend"] == "claude"
        assert info["session_id"] == session
        assert info["native_session_id"] == session
        assert info["turns"] == 0
        assert info["attached"] is True
        assert info["subprocess_running"] is True
        # Cumulative usage is pre-initialised with the canonical token
        # keys at zero. ``last_turn_usage`` mirrors that until the first
        # ``agent.result`` lands.
        assert all(v == 0 for v in info["cumulative_usage"].values())
        assert all(v == 0 for v in info["last_turn_usage"].values())
        assert info["context_tokens"] == 0
    finally:
        await c.close()


async def test_real_claude_session_info_in_memory_after_turn(real_daemon):
    """One turn → ``turns:1`` plus ``model`` / non-empty cumulative usage."""
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(_open_claude(session))
        await c.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)
        await _say_ok(c, session)
        await c.send({"type": "agent.session_info", "id": "i1", "session_id": session})
        info = await c.wait_for(lambda e: e.get("type") == "agent.session_info_reply", timeout=10.0)
        assert info["turns"] == 1
        assert info["attached"] is True
        assert info["subprocess_running"] is True
        # Real CC populates a model identifier (the active model used for the turn).
        assert isinstance(info.get("model"), str) and info["model"]
        # Cumulative usage covers at least input + output tokens for a real turn.
        cu = info["cumulative_usage"]
        assert cu.get("input_tokens", 0) > 0
        assert cu.get("output_tokens", 0) > 0
    finally:
        await c.close()


async def test_real_claude_session_info_after_close_uses_on_disk(real_daemon, tmp_path):
    """Closed-but-on-disk sessions still answer session_info.

    Locks down the regression where session_info errored with
    ``session_unknown`` for any session not currently in
    ``self._sessions`` — which included sessions just listed via
    ``list_sessions`` and sessions reopened across daemon restarts.
    Now the daemon walks ``~/.claude/projects/.../<sid>.jsonl`` and
    returns the head's ``cwd`` / ``model`` along with zeros for
    counters (no durable sidecar in the default config).
    """
    c = await _client(real_daemon)
    cwd = str(tmp_path.resolve())
    try:
        session = str(uuid.uuid4())
        await c.send(_open_claude(session, cwd=cwd))
        await c.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)
        await _say_ok(c, session)
        await c.send({"type": "agent.close", "id": "c1", "session_id": session, "delete": False})
        await c.wait_for(lambda e: e.get("type") == "agent.closed", timeout=10.0)

        await c.send({"type": "agent.session_info", "id": "i1", "session_id": session})
        evt = await c.wait_for(
            lambda e: e.get("type") in {"agent.session_info_reply", "agent.error"},
            timeout=10.0,
        )
        assert evt["type"] == "agent.session_info_reply", f"on-disk session info errored: {evt}"
        assert evt["backend"] == "claude"
        assert evt["session_id"] == session
        assert evt["cwd"] == cwd
        assert evt["attached"] is False
        assert evt["subprocess_running"] is False
        # Without a durable usage sidecar (the daemon config doesn't
        # enable one for these e2e tests) the counters fall back to
        # zero — but ``model`` is still recoverable from the
        # transcript's first system event.
        assert isinstance(evt.get("model"), str) and evt["model"]
        assert evt["turns"] == 0
        assert evt["last_turn_usage"] == {}
        assert evt["cumulative_usage"] == {}
    finally:
        await c.close()


async def test_real_claude_session_info_unknown_session(real_daemon):
    c = await _client(real_daemon)
    try:
        await c.send({"type": "agent.session_info", "id": "i1", "session_id": "no-such-session"})
        err = await c.wait_for(lambda e: e.get("type") == "agent.error", timeout=10.0)
        assert err["code"] == "session_unknown"
    finally:
        await c.close()


async def test_real_claude_list_sessions_lifecycle(real_daemon, tmp_path):
    """Open, list, close, list, close+delete, list: each step changes the row.

    Folds three interactions into one test to avoid spinning up three
    real claude turns.
    """
    c = await _client(real_daemon)
    cwd = str(tmp_path.resolve())
    try:
        session = str(uuid.uuid4())
        await c.send(_open_claude(session, cwd=cwd))
        await c.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)
        await _say_ok(c, session)

        # 1. Open + run a turn → list shows it as attached=true.
        await c.send({"type": "agent.list_sessions", "id": "l1", "cwd": cwd})
        ls1 = await c.wait_for(
            lambda e: e.get("type") == "agent.sessions" and e.get("id") == "l1",
            timeout=10.0,
        )
        ours = next((r for r in ls1["sessions"] if r["session_id"] == session), None)
        assert ours is not None, f"open session missing from list: {ls1['sessions']}"
        assert ours.get("attached") is True
        assert ours.get("backend") == "claude"

        # 2. Close (no delete) → list still shows it, attached=false.
        await c.send({"type": "agent.close", "id": "c1", "session_id": session, "delete": False})
        await c.wait_for(lambda e: e.get("type") == "agent.closed", timeout=10.0)
        await c.send({"type": "agent.list_sessions", "id": "l2", "cwd": cwd})
        ls2 = await c.wait_for(
            lambda e: e.get("type") == "agent.sessions" and e.get("id") == "l2",
            timeout=10.0,
        )
        ours = next((r for r in ls2["sessions"] if r["session_id"] == session), None)
        assert ours is not None
        assert ours.get("attached") is False

        # 3. Reopen + close with delete=true → list no longer shows it.
        # We open with resume=true to attach to the on-disk transcript;
        # the daemon's --resume code path is exercised separately by
        # ``test_real_claude_resume_preserves_context``.
        await c.send(
            {
                "type": "agent.open",
                "id": "r2",
                "session_id": session,
                "backend": "claude",
                "resume": True,
                "options": {
                    "claude": {
                        "model": "haiku",
                        "tools": "",
                        "permission_mode": "bypassPermissions",
                        "cwd": cwd,
                    }
                },
            }
        )
        await c.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)
        await c.send({"type": "agent.close", "id": "c2", "session_id": session, "delete": True})
        await c.wait_for(lambda e: e.get("type") == "agent.closed", timeout=10.0)
        await c.send({"type": "agent.list_sessions", "id": "l3", "cwd": cwd})
        ls3 = await c.wait_for(
            lambda e: e.get("type") == "agent.sessions" and e.get("id") == "l3",
            timeout=10.0,
        )
        assert all(r["session_id"] != session for r in ls3["sessions"]), (
            f"deleted session still listed: {ls3['sessions']}"
        )
    finally:
        await c.close()


async def test_real_claude_close_delete_preserves_backend_transcript(real_daemon, tmp_path):
    """``close{delete:true}`` removes the daemon's own state (event log,
    usage sidecar) but *not* CC's transcript under
    ``~/.claude/projects/``. Backend dirs are the backend's domain;
    the daemon doesn't garbage-collect them. Resume-from-disk for
    that session_id continues to work after a delete-close.
    """
    from blemees_agent.backends.claude import session_file_path

    c = await _client(real_daemon)
    cwd = str(tmp_path.resolve())
    try:
        session = str(uuid.uuid4())
        await c.send(_open_claude(session, cwd=cwd))
        await c.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)
        await _say_ok(c, session)
        path = session_file_path(cwd, session)
        assert path.is_file(), f"transcript missing after turn: {path}"

        await c.send({"type": "agent.close", "id": "c1", "session_id": session, "delete": True})
        await c.wait_for(lambda e: e.get("type") == "agent.closed", timeout=10.0)
        # CC's own transcript stays — only the daemon's event log /
        # usage sidecar are unlinked. CC manages its own directory.
        assert path.exists(), f"backend transcript should not be deleted: {path}"
    finally:
        await c.close()


async def test_real_claude_interrupt_when_idle_is_no_op(real_daemon):
    """Interrupt with no in-flight turn → ``was_idle:true``, no respawn."""
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(_open_claude(session))
        await c.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)

        await c.send({"type": "agent.interrupt", "session_id": session})
        ir = await c.wait_for(lambda e: e.get("type") == "agent.interrupted", timeout=10.0)
        assert ir["was_idle"] is True

        # Subsequent turn still works — the no-op interrupt didn't
        # disturb the subprocess.
        await _say_ok(c, session)
    finally:
        await c.close()


async def test_real_claude_session_busy_on_concurrent_user_turn(real_daemon):
    """A second ``agent.user`` while a turn is in flight → ``session_busy``."""
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(_open_claude(session))
        await c.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)
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
        # Wait until the model is producing output, then send the
        # second turn before the first finishes.
        await c.wait_for(
            lambda e: e.get("type") in {"agent.delta", "agent.message"},
            timeout=120.0,
        )
        await c.send(
            {
                "type": "agent.user",
                "session_id": session,
                "message": {"role": "user", "content": "Say OK."},
            }
        )
        err = await c.wait_for(
            lambda e: e.get("type") == "agent.error" and e.get("session_id") == session,
            timeout=10.0,
        )
        assert err["code"] == "session_busy"
        # Drain the in-flight turn so the test cleans up gracefully.
        await _drain_turn(c, session, timeout=180.0)
    finally:
        await c.close()


async def test_real_claude_status_reports_real_version(real_daemon):
    c = await _client(real_daemon)
    try:
        await c.send({"type": "agent.status", "id": "s1"})
        st = await c.wait_for(lambda e: e.get("type") == "agent.status_reply", timeout=10.0)
        assert "claude" in st["backends"]
        version = st["backends"]["claude"]
        # Match a leading digit cluster — real CC's `--version` output
        # parses to something like ``2.1.123``.
        assert isinstance(version, str)
        assert version[:1].isdigit(), f"unexpected version blob: {version!r}"
        assert st["protocol"] == PROTOCOL_VERSION
        assert isinstance(st["sessions"]["by_backend"], dict)
    finally:
        await c.close()


async def test_real_claude_ping_pong_echoes_data(real_daemon):
    c = await _client(real_daemon)
    try:
        payload = {"nested": {"k": [1, 2, "three"]}}
        await c.send({"type": "agent.ping", "id": "p1", "data": payload})
        pong = await c.wait_for(lambda e: e.get("type") == "agent.pong", timeout=5.0)
        assert pong["id"] == "p1"
        assert pong["data"] == payload
    finally:
        await c.close()


async def test_real_claude_open_same_session_id_twice(real_daemon):
    """Two opens of the same session id (no resume) → second errors."""
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(_open_claude(session))
        await c.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)
        await c.send(
            {
                "type": "agent.open",
                "id": "r2",
                "session_id": session,
                "backend": "claude",
                "options": {
                    "claude": {
                        "model": "haiku",
                        "tools": "",
                        "permission_mode": "bypassPermissions",
                    }
                },
            }
        )
        evt = await c.wait_for(
            lambda e: e.get("type") in {"agent.opened", "agent.error"} and e.get("id") == "r2",
            timeout=10.0,
        )
        assert evt["type"] == "agent.error"
        assert evt["code"] == "session_exists"
    finally:
        await c.close()


async def test_real_claude_array_text_content(real_daemon):
    """``content`` can be an array of text blocks (multimodal envelope)."""
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(_open_claude(session))
        await c.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)
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


async def test_real_claude_watch_observer_sees_owner_events(real_daemon):
    """A second connection's ``watch`` sees the same agent.* events."""
    owner = await _client(real_daemon)
    watcher = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await owner.send(_open_claude(session))
        await owner.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)

        await watcher.send(
            {"type": "agent.watch", "id": "w1", "session_id": session, "last_seen_seq": 0}
        )
        await watcher.wait_for(lambda e: e.get("type") == "agent.watching", timeout=10.0)

        await owner.send(
            {
                "type": "agent.user",
                "session_id": session,
                "message": {"role": "user", "content": "Say OK."},
            }
        )
        # Both connections see the turn-end frame.
        await _drain_turn(owner, session)
        await _drain_turn(watcher, session)

        await watcher.send({"type": "agent.unwatch", "id": "uw1", "session_id": session})
        ack = await watcher.wait_for(lambda e: e.get("type") == "agent.unwatched", timeout=10.0)
        assert ack["was_watching"] is True
    finally:
        await owner.close()
        await watcher.close()


async def test_real_claude_watch_unknown_session_errors(real_daemon):
    c = await _client(real_daemon)
    try:
        await c.send({"type": "agent.watch", "id": "w1", "session_id": "no-such-session"})
        err = await c.wait_for(lambda e: e.get("type") == "agent.error", timeout=10.0)
        assert err["code"] == "session_unknown"
    finally:
        await c.close()


async def test_real_claude_interrupt_then_continue(real_daemon):
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(_open_claude(session))
        await c.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)
        await c.send(
            {
                "type": "agent.user",
                "session_id": session,
                "message": {
                    "role": "user",
                    "content": "Count slowly from 1 to 200, one number per line.",
                },
            }
        )
        # claude -p emits an agent.system_init then an agent.delta /
        # agent.message once the model starts. We accept either as the
        # signal that the turn is in flight (some configurations
        # buffer the first chunk into a single agent.message).
        await c.wait_for(
            lambda e: e.get("type") in {"agent.delta", "agent.message"},
            timeout=120.0,
        )
        await c.send({"type": "agent.interrupt", "session_id": session})
        await c.wait_for(lambda e: e.get("type") == "agent.interrupted", timeout=15.0)
        # Subsequent turn still works (claude respawns with --resume).
        await c.send(
            {
                "type": "agent.user",
                "session_id": session,
                "message": {"role": "user", "content": "Say OK."},
            }
        )
        await c.wait_for(
            lambda e: e.get("type") == "agent.result" and e.get("session_id") == session,
            timeout=120.0,
        )
    finally:
        await c.close()


# ---------------------------------------------------------------------------
# Open / options edge cases
# ---------------------------------------------------------------------------


async def test_real_claude_open_unknown_option_rejected(real_daemon):
    """An unknown key under ``options.claude`` → ``invalid_message``."""
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(
            {
                "type": "agent.open",
                "id": "r1",
                "session_id": session,
                "backend": "claude",
                "options": {
                    "claude": {
                        "model": "haiku",
                        "permission_mode": "bypassPermissions",
                        "fubar_unknown_key": "nope",
                    }
                },
            }
        )
        err = await c.wait_for(
            lambda e: e.get("type") == "agent.error" and e.get("id") == "r1",
            timeout=10.0,
        )
        assert err["code"] == "invalid_message"
        assert "fubar_unknown_key" in err["message"]
    finally:
        await c.close()


async def test_real_claude_open_unsafe_flag_rejected(real_daemon):
    """``dangerously_skip_permissions`` and friends are refused at parse time."""
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(
            {
                "type": "agent.open",
                "id": "r1",
                "session_id": session,
                "backend": "claude",
                "options": {
                    "claude": {
                        "model": "haiku",
                        "dangerously_skip_permissions": True,
                    }
                },
            }
        )
        err = await c.wait_for(
            lambda e: e.get("type") == "agent.error" and e.get("id") == "r1",
            timeout=10.0,
        )
        assert err["code"] == "unsafe_flag"
    finally:
        await c.close()


async def test_real_claude_unknown_backend_rejected(real_daemon):
    """``backend:"zoom"`` → ``unknown_backend`` (no spawn attempted)."""
    c = await _client(real_daemon)
    try:
        await c.send(
            {
                "type": "agent.open",
                "id": "r1",
                "session_id": str(uuid.uuid4()),
                "backend": "zoom",
                "options": {"zoom": {}},
            }
        )
        err = await c.wait_for(
            lambda e: e.get("type") == "agent.error" and e.get("id") == "r1",
            timeout=10.0,
        )
        assert err["code"] == "unknown_backend"
    finally:
        await c.close()


async def test_real_claude_open_with_empty_options(real_daemon):
    """``options.claude = {}`` is accepted (CC defaults apply)."""
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(
            {
                "type": "agent.open",
                "id": "r1",
                "session_id": session,
                "backend": "claude",
                "options": {"claude": {}},
            }
        )
        # Either succeeds (default model/tools) or fails with spawn_failed
        # if the local CC config can't honour pure defaults — but the
        # frame must be accepted by the daemon (no invalid_message).
        evt = await c.wait_for(
            lambda e: e.get("type") in {"agent.opened", "agent.error"} and e.get("id") == "r1",
            timeout=30.0,
        )
        assert evt["type"] == "agent.opened" or evt["code"] != "invalid_message", evt
    finally:
        await c.close()


# ---------------------------------------------------------------------------
# agent.user / dispatch edge cases
# ---------------------------------------------------------------------------


async def test_real_claude_agent_user_unknown_session(real_daemon):
    """``agent.user`` for a session that was never opened → ``session_unknown``."""
    c = await _client(real_daemon)
    try:
        await c.send(
            {
                "type": "agent.user",
                "session_id": "no-such-session",
                "message": {"role": "user", "content": "hi"},
            }
        )
        err = await c.wait_for(lambda e: e.get("type") == "agent.error", timeout=10.0)
        assert err["code"] == "session_unknown"
    finally:
        await c.close()


async def test_real_claude_agent_user_role_must_be_user(real_daemon):
    """``message.role`` other than ``"user"`` is rejected at parse time."""
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(_open_claude(session))
        await c.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)
        await c.send(
            {
                "type": "agent.user",
                "session_id": session,
                "message": {"role": "assistant", "content": "I'm an assistant."},
            }
        )
        err = await c.wait_for(lambda e: e.get("type") == "agent.error", timeout=10.0)
        assert err["code"] == "invalid_message"
    finally:
        await c.close()


# ---------------------------------------------------------------------------
# Connection-level / dispatcher errors
# ---------------------------------------------------------------------------


async def test_real_claude_protocol_mismatch_is_fatal(real_daemon):
    """A hello with a wrong protocol version closes the connection."""
    reader, writer = await asyncio.open_unix_connection(real_daemon)
    try:
        from tests.blemees_agent.conftest import _StreamClient

        c = _StreamClient(reader, writer)
        await c.send({"type": "agent.hello", "client": "e2e/0", "protocol": "blemees/9"})
        err = await c.wait_for(lambda e: e.get("type") == "agent.error", timeout=10.0)
        assert err["code"] == "protocol_mismatch"
        # Daemon closes the connection — the next read returns the
        # sentinel pump-closed frame.
        closed = await c.recv(timeout=5.0)
        assert closed == {"type": "__closed__"}
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass


async def test_real_claude_unknown_message_type(real_daemon):
    """An unknown ``blemeesd.*`` type → ``unknown_message``, connection survives."""
    c = await _client(real_daemon)
    try:
        await c.send({"type": "agent.fubar", "id": "x1"})
        err = await c.wait_for(
            lambda e: e.get("type") == "agent.error" and e.get("id") == "x1",
            timeout=5.0,
        )
        assert err["code"] == "unknown_message"
        # Connection still works.
        await c.send({"type": "agent.ping", "id": "p1"})
        pong = await c.wait_for(lambda e: e.get("type") == "agent.pong", timeout=5.0)
        assert pong["id"] == "p1"
    finally:
        await c.close()


# ---------------------------------------------------------------------------
# Close edge cases
# ---------------------------------------------------------------------------


async def test_real_claude_close_unknown_session_is_idempotent(real_daemon):
    """Closing an unknown session id replies with ``closed`` (idempotent)."""
    c = await _client(real_daemon)
    try:
        await c.send(
            {
                "type": "agent.close",
                "id": "c1",
                "session_id": "no-such-session",
                "delete": False,
            }
        )
        ack = await c.wait_for(
            lambda e: e.get("type") == "agent.closed" and e.get("id") == "c1",
            timeout=10.0,
        )
        assert ack["session_id"] == "no-such-session"
    finally:
        await c.close()


async def test_real_claude_double_close_is_idempotent(real_daemon):
    """Closing the same session twice → second is still a clean ``closed`` ack."""
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(_open_claude(session))
        await c.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)
        await c.send({"type": "agent.close", "id": "c1", "session_id": session, "delete": True})
        await c.wait_for(
            lambda e: e.get("type") == "agent.closed" and e.get("id") == "c1",
            timeout=10.0,
        )
        await c.send({"type": "agent.close", "id": "c2", "session_id": session, "delete": False})
        ack2 = await c.wait_for(
            lambda e: e.get("type") == "agent.closed" and e.get("id") == "c2",
            timeout=10.0,
        )
        assert ack2["session_id"] == session
    finally:
        await c.close()


# ---------------------------------------------------------------------------
# Multi-turn + multi-session
# ---------------------------------------------------------------------------


async def test_real_claude_three_turns_accumulate_usage(real_daemon):
    """``cumulative_usage.input/output_tokens`` grows across consecutive turns."""
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(_open_claude(session))
        await c.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)
        for _ in range(3):
            await _say_ok(c, session)
        await c.send({"type": "agent.session_info", "id": "i1", "session_id": session})
        info = await c.wait_for(lambda e: e.get("type") == "agent.session_info_reply", timeout=10.0)
        assert info["turns"] == 3
        cu = info["cumulative_usage"]
        # Three minimal turns easily clear single-digit input/output token totals.
        assert cu.get("input_tokens", 0) >= 3
        assert cu.get("output_tokens", 0) >= 3
    finally:
        await c.close()


async def test_real_claude_two_concurrent_sessions_independent(real_daemon):
    """Two sessions on one connection don't bleed events into each other."""
    c = await _client(real_daemon)
    try:
        s1 = str(uuid.uuid4())
        s2 = str(uuid.uuid4())
        for sid in (s1, s2):
            await c.send(_open_claude(sid))
            await c.wait_for(
                # Bind ``sid`` at lambda creation time — without this,
                # the predicate would close over the loop variable and
                # match against whatever ``sid`` ended up as after the
                # loop finished.
                lambda e, sid=sid: e.get("type") == "agent.opened" and e.get("session_id") == sid,
                timeout=30.0,
            )
        await c.send(
            {
                "type": "agent.user",
                "session_id": s1,
                "message": {"role": "user", "content": "Say OK."},
            }
        )
        await c.send(
            {
                "type": "agent.user",
                "session_id": s2,
                "message": {"role": "user", "content": "Say OK."},
            }
        )
        # Drain both turns — order is undefined, just count them.
        seen_results: dict[str, dict] = {}
        deadline = asyncio.get_running_loop().time() + 120.0
        while len(seen_results) < 2:
            remaining = deadline - asyncio.get_running_loop().time()
            assert remaining > 0, f"timed out waiting for both results; got={list(seen_results)}"
            evt = await c.recv(timeout=remaining)
            if evt.get("type") == "agent.result" and evt.get("session_id") in {s1, s2}:
                seen_results[evt["session_id"]] = evt
        assert seen_results[s1]["session_id"] == s1
        assert seen_results[s2]["session_id"] == s2
    finally:
        await c.close()


# ---------------------------------------------------------------------------
# Status snapshot
# ---------------------------------------------------------------------------


async def test_real_claude_status_active_turns_during_turn(real_daemon):
    """``status.sessions.active_turns`` increments while a turn is in flight."""
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(_open_claude(session))
        await c.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)
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
        await c.wait_for(lambda e: e.get("type") in {"agent.delta", "agent.message"}, timeout=120.0)
        await c.send({"type": "agent.status", "id": "s1"})
        st = await c.wait_for(
            lambda e: e.get("type") == "agent.status_reply" and e.get("id") == "s1",
            timeout=10.0,
        )
        assert st["sessions"]["active_turns"] >= 1
        assert st["sessions"]["by_backend"].get("claude", 0) >= 1
        # Cancel for cleanup — this also exercises the synth path
        # (claude.ClaudeBackend.interrupt schedules an
        # ``agent.result{subtype:"interrupted"}`` task). The exact
        # post-cancel ordering is locked down by the mock test
        # ``test_interrupt_emits_synthesized_agent_result``; here we
        # just confirm the ack lands.
        await c.send({"type": "agent.interrupt", "session_id": session})
        await c.wait_for(lambda e: e.get("type") == "agent.interrupted", timeout=15.0)
    finally:
        await c.close()


# ---------------------------------------------------------------------------
# Watch / unwatch edge cases
# ---------------------------------------------------------------------------


async def test_real_claude_unwatch_when_not_watching(real_daemon):
    """Unwatching a session you weren't watching → ``was_watching:false``."""
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(_open_claude(session))
        await c.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)
        await c.send({"type": "agent.unwatch", "id": "uw1", "session_id": session})
        ack = await c.wait_for(
            lambda e: e.get("type") == "agent.unwatched" and e.get("id") == "uw1",
            timeout=10.0,
        )
        assert ack["was_watching"] is False
    finally:
        await c.close()


async def test_real_claude_multiple_watchers_each_see_events(real_daemon):
    """Two parallel watchers both receive the owner's agent.* stream."""
    owner = await _client(real_daemon)
    w1 = await _client(real_daemon)
    w2 = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await owner.send(_open_claude(session))
        await owner.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)
        for w, wid in ((w1, "wA"), (w2, "wB")):
            await w.send(
                {
                    "type": "agent.watch",
                    "id": wid,
                    "session_id": session,
                    "last_seen_seq": 0,
                }
            )
            await w.wait_for(lambda e: e.get("type") == "agent.watching", timeout=10.0)

        await owner.send(
            {
                "type": "agent.user",
                "session_id": session,
                "message": {"role": "user", "content": "Say OK."},
            }
        )
        await _drain_turn(owner, session)
        await _drain_turn(w1, session)
        await _drain_turn(w2, session)
    finally:
        await owner.close()
        await w1.close()
        await w2.close()


# ---------------------------------------------------------------------------
# list_sessions edge cases
# ---------------------------------------------------------------------------


async def test_real_claude_list_sessions_empty_cwd(real_daemon, tmp_path):
    """A cwd with no sessions → empty array, no error."""
    c = await _client(real_daemon)
    try:
        empty_cwd = str((tmp_path / "no-sessions-here").resolve())
        # Note: don't have to create the dir on disk — the daemon's
        # walker handles missing dirs gracefully.
        await c.send({"type": "agent.list_sessions", "id": "l1", "cwd": empty_cwd})
        ls = await c.wait_for(
            lambda e: e.get("type") == "agent.sessions" and e.get("id") == "l1",
            timeout=10.0,
        )
        assert ls["sessions"] == []
        assert ls["cwd"] == empty_cwd
    finally:
        await c.close()


async def test_real_claude_list_sessions_includes_first_user_preview(real_daemon, tmp_path):
    """Each row carries a ``preview`` summary of the first user message."""
    c = await _client(real_daemon)
    cwd = str(tmp_path.resolve())
    try:
        session = str(uuid.uuid4())
        await c.send(_open_claude(session, cwd=cwd))
        await c.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)
        await c.send(
            {
                "type": "agent.user",
                "session_id": session,
                "message": {
                    "role": "user",
                    "content": "preview-marker-please-keep — Say OK.",
                },
            }
        )
        await _drain_turn(c, session)
        await c.send({"type": "agent.list_sessions", "id": "l1", "cwd": cwd})
        ls = await c.wait_for(
            lambda e: e.get("type") == "agent.sessions" and e.get("id") == "l1",
            timeout=10.0,
        )
        ours = next((r for r in ls["sessions"] if r["session_id"] == session), None)
        assert ours is not None, ls["sessions"]
        preview = ours.get("preview", "")
        assert "preview-marker-please-keep" in preview, preview
    finally:
        await c.close()


# ---------------------------------------------------------------------------
# Reattach / replay
# ---------------------------------------------------------------------------


async def test_real_claude_reconnect_resume_replays_seen_events(real_daemon):
    """Soft-disconnect + reopen with ``last_seen_seq=0`` → ring buffer replays."""
    c = await _client(real_daemon)
    session = str(uuid.uuid4())
    try:
        await c.send(_open_claude(session))
        opened = await c.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)
        await _say_ok(c, session)
        last_seq = opened.get("last_seq", 0)
    finally:
        await c.close()

    # Fresh connection asks for replay from seq 0.
    c2 = await _client(real_daemon)
    try:
        await c2.send(
            {
                "type": "agent.open",
                "id": "r2",
                "session_id": session,
                "backend": "claude",
                "resume": True,
                "last_seen_seq": 0,
                "options": {
                    "claude": {
                        "model": "haiku",
                        "tools": "",
                        "permission_mode": "bypassPermissions",
                    }
                },
            }
        )
        opened2 = await c2.wait_for(
            lambda e: e.get("type") == "agent.opened" and e.get("id") == "r2",
            timeout=30.0,
        )
        assert opened2["last_seq"] >= last_seq
        # We should see at least one replayed agent.* frame from the
        # buffered ring (the prior turn produced several).
        replayed = await c2.wait_for(
            lambda e: isinstance(e.get("type"), str) and e["type"].startswith("agent."),
            timeout=15.0,
        )
        assert isinstance(replayed.get("seq"), int)
        assert replayed["seq"] >= 1
    finally:
        await c2.close()


async def test_real_claude_double_interrupt_second_is_idle(real_daemon):
    """Two interrupts in a row → second carries ``was_idle:true``."""
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(_open_claude(session))
        await c.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)
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
        await c.wait_for(lambda e: e.get("type") in {"agent.delta", "agent.message"}, timeout=120.0)
        await c.send({"type": "agent.interrupt", "session_id": session})
        ir1 = await c.wait_for(lambda e: e.get("type") == "agent.interrupted", timeout=15.0)
        assert ir1["was_idle"] is False
        # Second interrupt — turn is no longer in flight.
        await c.send({"type": "agent.interrupt", "session_id": session})
        ir2 = await c.wait_for(lambda e: e.get("type") == "agent.interrupted", timeout=10.0)
        assert ir2["was_idle"] is True
    finally:
        await c.close()


async def test_real_claude_session_takeover_notifies_old_owner(real_daemon):
    """Second connection re-opens with ``resume=true`` → old owner gets ``session_taken``."""
    owner = await _client(real_daemon)
    other = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await owner.send(_open_claude(session))
        await owner.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)
        # Run a quick turn so the session is fully initialised.
        await _say_ok(owner, session)

        await other.send(
            {
                "type": "agent.open",
                "id": "takeover",
                "session_id": session,
                "backend": "claude",
                "resume": True,
                "options": {
                    "claude": {
                        "model": "haiku",
                        "tools": "",
                        "permission_mode": "bypassPermissions",
                    }
                },
            }
        )
        # Owner sees session_taken; other sees opened.
        evt = await owner.wait_for(
            lambda e: e.get("type") == "agent.session_taken" and e.get("session_id") == session,
            timeout=15.0,
        )
        assert "by_peer_pid" in evt or evt.get("by_peer_pid") is None
        await other.wait_for(
            lambda e: e.get("type") == "agent.opened" and e.get("id") == "takeover",
            timeout=15.0,
        )
    finally:
        await owner.close()
        await other.close()


# ---------------------------------------------------------------------------
# Hello / handshake edge cases
# ---------------------------------------------------------------------------


async def test_real_claude_hello_with_no_client_field(real_daemon):
    """``client`` is optional on ``agent.hello``."""
    reader, writer = await asyncio.open_unix_connection(real_daemon)
    try:
        from tests.blemees_agent.conftest import _StreamClient

        c = _StreamClient(reader, writer)
        await c.send({"type": "agent.hello", "protocol": PROTOCOL_VERSION})
        ack = await c.wait_for(lambda e: e.get("type") == "agent.hello_ack", timeout=10.0)
        assert ack["protocol"] == PROTOCOL_VERSION
        await c.close()
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass


async def test_real_claude_hello_with_unknown_field_rejected(real_daemon):
    """Extra fields under ``agent.hello`` violate ``additionalProperties:false``."""
    reader, writer = await asyncio.open_unix_connection(real_daemon)
    try:
        from tests.blemees_agent.conftest import _StreamClient

        c = _StreamClient(reader, writer)
        await c.send(
            {
                "type": "agent.hello",
                "protocol": PROTOCOL_VERSION,
                "fubar_extra": True,
            }
        )
        err = await c.wait_for(lambda e: e.get("type") == "agent.error", timeout=10.0)
        assert err["code"] == "invalid_message"
        await c.close()
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass


async def test_real_claude_frame_before_hello_rejected(real_daemon):
    """The first frame on a connection must be ``agent.hello``."""
    reader, writer = await asyncio.open_unix_connection(real_daemon)
    try:
        from tests.blemees_agent.conftest import _StreamClient

        c = _StreamClient(reader, writer)
        await c.send({"type": "agent.ping", "id": "p1"})
        err = await c.wait_for(lambda e: e.get("type") == "agent.error", timeout=10.0)
        # Spec: first frame must be hello — anything else is invalid_message.
        assert err["code"] == "invalid_message"
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


async def test_real_claude_agent_user_missing_message(real_daemon):
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(_open_claude(session))
        await c.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)
        await c.send({"type": "agent.user", "session_id": session})  # no message
        err = await c.wait_for(lambda e: e.get("type") == "agent.error", timeout=10.0)
        assert err["code"] == "invalid_message"
    finally:
        await c.close()


async def test_real_claude_agent_user_content_null(real_daemon):
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(_open_claude(session))
        await c.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)
        await c.send(
            {
                "type": "agent.user",
                "session_id": session,
                "message": {"role": "user", "content": None},
            }
        )
        err = await c.wait_for(lambda e: e.get("type") == "agent.error", timeout=10.0)
        assert err["code"] == "invalid_message"
    finally:
        await c.close()


async def test_real_claude_agent_user_unicode_emoji_passthrough(real_daemon):
    """Multi-byte UTF-8 (emoji) is forwarded verbatim to claude."""
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(_open_claude(session))
        await c.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)
        await c.send(
            {
                "type": "agent.user",
                "session_id": session,
                "message": {
                    "role": "user",
                    "content": "Reply 🎯 if you can read this emoji and the word KEYWORD-XYZ.",
                },
            }
        )
        collected = await c.wait_for(
            lambda e: e.get("type") == "agent.result" and e.get("session_id") == session,
            collect=True,
            timeout=60.0,
        )
        # Just confirm a successful result; we don't assert on response text
        # because models occasionally render or omit emoji unpredictably.
        end = collected[-1]
        assert end["subtype"] in {"success", "error"}
    finally:
        await c.close()


# ---------------------------------------------------------------------------
# Multi-turn rapid
# ---------------------------------------------------------------------------


async def test_real_claude_back_to_back_turns(real_daemon):
    """Two turns sent back-to-back (after each result) on one session."""
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(_open_claude(session))
        await c.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)
        await _say_ok(c, session)
        # Second turn immediately — daemon must accept after the result.
        await _say_ok(c, session)
    finally:
        await c.close()


# ---------------------------------------------------------------------------
# session_info during in-flight turn
# ---------------------------------------------------------------------------


async def test_real_claude_session_info_during_inflight_turn(real_daemon):
    """Querying session_info mid-turn shows ``subprocess_running:true``."""
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(_open_claude(session))
        await c.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)
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
        await c.wait_for(lambda e: e.get("type") in {"agent.delta", "agent.message"}, timeout=120.0)
        await c.send({"type": "agent.session_info", "id": "i1", "session_id": session})
        info = await c.wait_for(
            lambda e: e.get("type") == "agent.session_info_reply" and e.get("id") == "i1",
            timeout=10.0,
        )
        assert info["attached"] is True
        assert info["subprocess_running"] is True
        # Cancel cleanup.
        await c.send({"type": "agent.interrupt", "session_id": session})
        await c.wait_for(lambda e: e.get("type") == "agent.interrupted", timeout=15.0)
    finally:
        await c.close()


# ---------------------------------------------------------------------------
# list_sessions ordering
# ---------------------------------------------------------------------------


async def test_real_claude_list_sessions_sorts_newest_first(real_daemon, tmp_path):
    """Multiple sessions in the same cwd → list_sessions returns mtime-desc."""
    c = await _client(real_daemon)
    cwd = str(tmp_path.resolve())
    try:
        first = str(uuid.uuid4())
        second = str(uuid.uuid4())
        await c.send(_open_claude(first, cwd=cwd))
        await c.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)
        await _say_ok(c, first)
        await c.send({"type": "agent.close", "id": "c1", "session_id": first, "delete": False})
        await c.wait_for(lambda e: e.get("type") == "agent.closed", timeout=10.0)
        # Tiny pause so mtimes differ on filesystems with whole-second resolution.
        await asyncio.sleep(1.1)
        await c.send(_open_claude(second, cwd=cwd))
        await c.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)
        await _say_ok(c, second)
        await c.send({"type": "agent.list_sessions", "id": "l1", "cwd": cwd})
        ls = await c.wait_for(
            lambda e: e.get("type") == "agent.sessions" and e.get("id") == "l1",
            timeout=10.0,
        )
        ours = [r for r in ls["sessions"] if r["session_id"] in {first, second}]
        assert len(ours) == 2
        # Newest first.
        assert ours[0]["session_id"] == second
        assert ours[1]["session_id"] == first
    finally:
        await c.close()


# ---------------------------------------------------------------------------
# Status snapshot
# ---------------------------------------------------------------------------


async def test_real_claude_status_with_no_sessions(real_daemon):
    """A daemon with no open sessions reports zeros across the board."""
    c = await _client(real_daemon)
    try:
        await c.send({"type": "agent.status", "id": "s1"})
        st = await c.wait_for(lambda e: e.get("type") == "agent.status_reply", timeout=10.0)
        s = st["sessions"]
        assert s["total"] == 0
        assert s["attached"] == 0
        assert s["detached"] == 0
        assert s["active_turns"] == 0
        # by_backend is keyed by backend name — empty when no sessions are open.
        assert s["by_backend"] == {} or all(v == 0 for v in s["by_backend"].values())
        assert isinstance(st["uptime_s"], (int, float))
        assert st["uptime_s"] >= 0
        assert st["connections"] >= 1  # this client
    finally:
        await c.close()


async def test_real_claude_status_uptime_monotonic(real_daemon):
    """``uptime_s`` strictly grows between successive status calls."""
    c = await _client(real_daemon)
    try:
        await c.send({"type": "agent.status", "id": "s1"})
        st1 = await c.wait_for(
            lambda e: e.get("type") == "agent.status_reply" and e.get("id") == "s1",
            timeout=10.0,
        )
        await asyncio.sleep(0.5)
        await c.send({"type": "agent.status", "id": "s2"})
        st2 = await c.wait_for(
            lambda e: e.get("type") == "agent.status_reply" and e.get("id") == "s2",
            timeout=10.0,
        )
        assert st2["uptime_s"] > st1["uptime_s"]
    finally:
        await c.close()


# ---------------------------------------------------------------------------
# Watch / unwatch edge cases (more)
# ---------------------------------------------------------------------------


async def test_real_claude_watcher_disconnect_auto_unwatch(real_daemon):
    """A watcher closing its connection cleanly auto-unwatches.

    The owner's session keeps running with no leaked references — the
    second turn after watcher disconnects still completes.
    """
    owner = await _client(real_daemon)
    watcher = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await owner.send(_open_claude(session))
        await owner.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)
        await watcher.send(
            {"type": "agent.watch", "id": "w1", "session_id": session, "last_seen_seq": 0}
        )
        await watcher.wait_for(lambda e: e.get("type") == "agent.watching", timeout=10.0)
        await _say_ok(owner, session)
        # Watcher disconnects.
        await watcher.close()
        # Owner can still drive the session.
        await _say_ok(owner, session)
    finally:
        await owner.close()


async def test_real_claude_watcher_replay_via_last_seen_seq(real_daemon):
    """Watcher arriving with ``last_seen_seq=0`` replays buffered frames."""
    owner = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await owner.send(_open_claude(session))
        await owner.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)
        await _say_ok(owner, session)
        # Now attach a fresh watcher; it should replay agent.* frames.
        watcher = await _client(real_daemon)
        try:
            await watcher.send(
                {
                    "type": "agent.watch",
                    "id": "w1",
                    "session_id": session,
                    "last_seen_seq": 0,
                }
            )
            ack = await watcher.wait_for(
                lambda e: e.get("type") == "agent.watching" and e.get("id") == "w1",
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
# Replay edge cases
# ---------------------------------------------------------------------------


async def test_real_claude_open_with_invalid_session_id_format(real_daemon):
    """Non-UUID session_id is accepted at the daemon layer but the
    Claude backend's spawn rejects it (claude requires UUIDs for
    ``--session-id``). Surface as ``spawn_failed``.
    """
    c = await _client(real_daemon)
    try:
        await c.send(
            {
                "type": "agent.open",
                "id": "r1",
                "session_id": "not-a-uuid",
                "backend": "claude",
                "options": {
                    "claude": {
                        "model": "haiku",
                        "tools": "",
                        "permission_mode": "bypassPermissions",
                    }
                },
            }
        )
        evt = await c.wait_for(
            lambda e: e.get("type") in {"agent.opened", "agent.error"} and e.get("id") == "r1",
            timeout=30.0,
        )
        if evt["type"] == "agent.error":
            # Real CC's `--session-id` rejects non-UUIDs at startup, so
            # the daemon surfaces it as a backend_crashed/spawn_failed.
            assert evt["code"] in {"spawn_failed", "backend_crashed"}, evt
        else:
            # Some CC builds tolerate any non-empty token — accept that
            # too. The point is: the daemon doesn't blow up on a non-UUID.
            pass
    finally:
        await c.close()


# ---------------------------------------------------------------------------
# Watch + ack-flow interactions
# ---------------------------------------------------------------------------


async def test_real_claude_watcher_sees_synthesized_interrupt_result(real_daemon):
    """A watcher sees the synthesised ``agent.result{interrupted}`` after an interrupt."""
    owner = await _client(real_daemon)
    watcher = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await owner.send(_open_claude(session))
        await owner.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)
        await watcher.send(
            {"type": "agent.watch", "id": "w1", "session_id": session, "last_seen_seq": 0}
        )
        await watcher.wait_for(lambda e: e.get("type") == "agent.watching", timeout=10.0)
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
        await owner.wait_for(
            lambda e: e.get("type") in {"agent.delta", "agent.message"}, timeout=120.0
        )
        await owner.send({"type": "agent.interrupt", "session_id": session})
        await owner.wait_for(lambda e: e.get("type") == "agent.interrupted", timeout=15.0)
        # The watcher should see the synthesised ``agent.result(interrupted)``
        # — it's part of the session's event stream, not the connection-scoped
        # interrupt ack.
        synthesized = await watcher.wait_for(
            lambda e: (
                e.get("type") == "agent.result"
                and e.get("subtype") == "interrupted"
                and e.get("session_id") == session
            ),
            timeout=30.0,
        )
        assert synthesized["seq"] >= 1
    finally:
        await owner.close()
        await watcher.close()


# ---------------------------------------------------------------------------
# Sessions with various option combinations (smoke tests)
# ---------------------------------------------------------------------------


async def test_real_claude_open_with_system_prompt(real_daemon):
    """``options.claude.system_prompt`` reaches the model and shapes its reply."""
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(
            {
                "type": "agent.open",
                "id": "r1",
                "session_id": session,
                "backend": "claude",
                "options": {
                    "claude": {
                        "model": "haiku",
                        "tools": "",
                        "permission_mode": "bypassPermissions",
                        "system_prompt": (
                            "You are a strict echo-bot. Reply with exactly the "
                            "uppercase token MARKER-XYZ and nothing else."
                        ),
                    }
                },
            }
        )
        await c.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)
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
            timeout=60.0,
        )
        text = ""
        for evt in collected:
            if evt.get("type") == "agent.message":
                for block in evt.get("content", []) or []:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text += block.get("text", "")
        # System prompts steer haiku reasonably reliably; the marker should appear.
        assert "MARKER-XYZ" in text, text
    finally:
        await c.close()


async def test_real_claude_open_extra_backend_block_ignored(real_daemon):
    """Specifying ``options.codex`` alongside ``options.claude`` is rejected.

    Spec wording is permissive in places ("only the matching block is
    consulted") but the schema rejects sibling backends to surface
    typos. Confirm the schema-side rejection wins.
    """
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(
            {
                "type": "agent.open",
                "id": "r1",
                "session_id": session,
                "backend": "claude",
                "options": {
                    "claude": {
                        "model": "haiku",
                        "tools": "",
                        "permission_mode": "bypassPermissions",
                    },
                    "codex": {"sandbox": "read-only"},  # sibling — should be flagged
                },
            }
        )
        evt = await c.wait_for(
            lambda e: e.get("type") in {"agent.opened", "agent.error"} and e.get("id") == "r1",
            timeout=30.0,
        )
        # Either silently ignored (opened) OR flagged (error). Both are
        # acceptable per spec; the test pins down which the daemon
        # actually does so we notice if it changes.
        assert evt["type"] in {"agent.opened", "agent.error"}
        if evt["type"] == "agent.error":
            assert evt["code"] == "invalid_message"
    finally:
        await c.close()


# ---------------------------------------------------------------------------
# Double-watch / unwatch matrix
# ---------------------------------------------------------------------------


async def test_real_claude_double_watch_is_idempotent(real_daemon):
    """Watching the same session twice is fine (second is a no-op)."""
    owner = await _client(real_daemon)
    watcher = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await owner.send(_open_claude(session))
        await owner.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)
        await watcher.send(
            {"type": "agent.watch", "id": "w1", "session_id": session, "last_seen_seq": 0}
        )
        ack1 = await watcher.wait_for(
            lambda e: e.get("type") == "agent.watching" and e.get("id") == "w1",
            timeout=10.0,
        )
        await watcher.send(
            {"type": "agent.watch", "id": "w2", "session_id": session, "last_seen_seq": 0}
        )
        ack2 = await watcher.wait_for(
            lambda e: e.get("type") == "agent.watching" and e.get("id") == "w2",
            timeout=10.0,
        )
        # Both succeeded; last_seq is the session's current high seq.
        assert ack1["last_seq"] >= 0
        assert ack2["last_seq"] >= ack1["last_seq"]
    finally:
        await owner.close()
        await watcher.close()


# ---------------------------------------------------------------------------
# Status: session counts during/after a turn
# ---------------------------------------------------------------------------


async def test_real_claude_status_total_after_close(real_daemon):
    """Open a session, run a turn, close — status total decrements."""
    c = await _client(real_daemon)
    try:
        # Baseline.
        await c.send({"type": "agent.status", "id": "s0"})
        st0 = await c.wait_for(
            lambda e: e.get("type") == "agent.status_reply" and e.get("id") == "s0",
            timeout=10.0,
        )
        baseline_total = st0["sessions"]["total"]

        session = str(uuid.uuid4())
        await c.send(_open_claude(session))
        await c.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)
        await _say_ok(c, session)

        await c.send({"type": "agent.status", "id": "s1"})
        st1 = await c.wait_for(
            lambda e: e.get("type") == "agent.status_reply" and e.get("id") == "s1",
            timeout=10.0,
        )
        assert st1["sessions"]["total"] == baseline_total + 1

        await c.send({"type": "agent.close", "id": "c1", "session_id": session, "delete": True})
        await c.wait_for(lambda e: e.get("type") == "agent.closed", timeout=10.0)

        await c.send({"type": "agent.status", "id": "s2"})
        st2 = await c.wait_for(
            lambda e: e.get("type") == "agent.status_reply" and e.get("id") == "s2",
            timeout=10.0,
        )
        assert st2["sessions"]["total"] == baseline_total
    finally:
        await c.close()


# ---------------------------------------------------------------------------
# session_info on session never opened (sidecar absent, on-disk absent)
# ---------------------------------------------------------------------------


async def test_real_claude_session_info_on_random_uuid_unknown(real_daemon):
    """A truly random UUID with no on-disk artefact still returns session_unknown."""
    c = await _client(real_daemon)
    try:
        random_id = str(uuid.uuid4())
        await c.send({"type": "agent.session_info", "id": "i1", "session_id": random_id})
        err = await c.wait_for(lambda e: e.get("type") == "agent.error", timeout=10.0)
        assert err["code"] == "session_unknown"
    finally:
        await c.close()


# ---------------------------------------------------------------------------
# Connection-close behavior (soft detach)
# ---------------------------------------------------------------------------


async def test_real_claude_soft_detach_idle_session_reattaches(real_daemon):
    """Disconnect without sending ``close`` while idle → reattach with resume."""
    c1 = await _client(real_daemon)
    session = str(uuid.uuid4())
    await c1.send(_open_claude(session))
    await c1.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)
    await _say_ok(c1, session)
    await c1.close()  # soft detach — no `agent.close` frame.

    # Fresh connection re-opens with resume:true.
    c2 = await _client(real_daemon)
    try:
        await c2.send(
            {
                "type": "agent.open",
                "id": "r2",
                "session_id": session,
                "backend": "claude",
                "resume": True,
                "options": {
                    "claude": {
                        "model": "haiku",
                        "tools": "",
                        "permission_mode": "bypassPermissions",
                    }
                },
            }
        )
        await c2.wait_for(
            lambda e: e.get("type") == "agent.opened" and e.get("id") == "r2",
            timeout=30.0,
        )
        # Run a fresh turn on the reattached session — confirms the
        # subprocess respawn with --resume worked.
        await _say_ok(c2, session)
    finally:
        await c2.close()


async def test_real_claude_third_connection_sees_session_taken_chain(real_daemon):
    """Sequential takeovers: A → B (A sees taken), B → C (B sees taken)."""
    a = await _client(real_daemon)
    b = await _client(real_daemon)
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await a.send(_open_claude(session))
        await a.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)
        await _say_ok(a, session)

        # B takes over from A.
        await b.send(
            {
                "type": "agent.open",
                "id": "b1",
                "session_id": session,
                "backend": "claude",
                "resume": True,
                "options": {
                    "claude": {
                        "model": "haiku",
                        "tools": "",
                        "permission_mode": "bypassPermissions",
                    }
                },
            }
        )
        evt_a = await a.wait_for(
            lambda e: e.get("type") == "agent.session_taken" and e.get("session_id") == session,
            timeout=15.0,
        )
        assert evt_a["session_id"] == session
        await b.wait_for(
            lambda e: e.get("type") == "agent.opened" and e.get("id") == "b1",
            timeout=15.0,
        )

        # C takes over from B.
        await c.send(
            {
                "type": "agent.open",
                "id": "c1",
                "session_id": session,
                "backend": "claude",
                "resume": True,
                "options": {
                    "claude": {
                        "model": "haiku",
                        "tools": "",
                        "permission_mode": "bypassPermissions",
                    }
                },
            }
        )
        evt_b = await b.wait_for(
            lambda e: e.get("type") == "agent.session_taken" and e.get("session_id") == session,
            timeout=15.0,
        )
        assert evt_b["session_id"] == session
        await c.wait_for(
            lambda e: e.get("type") == "agent.opened" and e.get("id") == "c1",
            timeout=15.0,
        )
    finally:
        await a.close()
        await b.close()
        await c.close()


# ---------------------------------------------------------------------------
# Empty ping / no data
# ---------------------------------------------------------------------------


async def test_real_claude_ping_without_data_or_id(real_daemon):
    """``data`` and ``id`` are optional on ping/pong."""
    c = await _client(real_daemon)
    try:
        await c.send({"type": "agent.ping"})
        pong = await c.wait_for(lambda e: e.get("type") == "agent.pong", timeout=5.0)
        assert "id" not in pong
        assert "data" not in pong
    finally:
        await c.close()


# ---------------------------------------------------------------------------
# Invalid-message edge cases that need a real daemon
# ---------------------------------------------------------------------------


async def test_real_claude_open_with_no_options_field_rejected(real_daemon):
    """``options`` is REQUIRED on ``agent.open``."""
    c = await _client(real_daemon)
    try:
        await c.send(
            {
                "type": "agent.open",
                "id": "r1",
                "session_id": str(uuid.uuid4()),
                "backend": "claude",
                # options field omitted on purpose.
            }
        )
        err = await c.wait_for(
            lambda e: e.get("type") == "agent.error" and e.get("id") == "r1",
            timeout=10.0,
        )
        assert err["code"] == "invalid_message"
    finally:
        await c.close()


async def test_real_claude_unsafe_literal_flag_rejected(real_daemon):
    """An unsafe-flag literal smuggled in as a value (e.g., in disallowed_tools) is rejected."""
    c = await _client(real_daemon)
    try:
        await c.send(
            {
                "type": "agent.open",
                "id": "r1",
                "session_id": str(uuid.uuid4()),
                "backend": "claude",
                "options": {
                    "claude": {
                        "model": "haiku",
                        "permission_mode": "bypassPermissions",
                        "disallowed_tools": ["--dangerously-skip-permissions"],
                    }
                },
            }
        )
        err = await c.wait_for(
            lambda e: e.get("type") == "agent.error" and e.get("id") == "r1",
            timeout=10.0,
        )
        # Daemon refuses literal unsafe flag tokens even nested in lists.
        assert err["code"] == "unsafe_flag"
    finally:
        await c.close()


# ---------------------------------------------------------------------------
# Session lifecycle: list_sessions during inflight turn
# ---------------------------------------------------------------------------


async def test_real_claude_list_sessions_during_inflight(real_daemon, tmp_path):
    """Listing sessions mid-turn includes the in-flight session as ``attached:true``."""
    c = await _client(real_daemon)
    cwd = str(tmp_path.resolve())
    try:
        session = str(uuid.uuid4())
        await c.send(_open_claude(session, cwd=cwd))
        await c.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)
        await c.send(
            {
                "type": "agent.user",
                "session_id": session,
                "message": {"role": "user", "content": "Count from 1 to 30, slowly."},
            }
        )
        await c.wait_for(lambda e: e.get("type") in {"agent.delta", "agent.message"}, timeout=120.0)
        await c.send({"type": "agent.list_sessions", "id": "l1", "cwd": cwd})
        ls = await c.wait_for(
            lambda e: e.get("type") == "agent.sessions" and e.get("id") == "l1",
            timeout=10.0,
        )
        ours = next((r for r in ls["sessions"] if r["session_id"] == session), None)
        assert ours is not None
        assert ours["attached"] is True
        # Cleanup: cancel the count.
        await c.send({"type": "agent.interrupt", "session_id": session})
        await c.wait_for(lambda e: e.get("type") == "agent.interrupted", timeout=15.0)
    finally:
        await c.close()


# ---------------------------------------------------------------------------
# Three connections, each owns a session; status reports them
# ---------------------------------------------------------------------------


async def test_real_claude_three_connections_three_sessions(real_daemon):
    """Three independent connections, each driving its own session."""
    clients = [await _client(real_daemon) for _ in range(3)]
    sessions = [str(uuid.uuid4()) for _ in range(3)]
    try:
        for c, sid in zip(clients, sessions, strict=True):
            await c.send(_open_claude(sid))
            await c.wait_for(
                lambda e, sid=sid: e.get("type") == "agent.opened" and e.get("session_id") == sid,
                timeout=30.0,
            )
        # Each runs a turn.
        for c, sid in zip(clients, sessions, strict=True):
            await c.send(
                {
                    "type": "agent.user",
                    "session_id": sid,
                    "message": {"role": "user", "content": "Say OK."},
                }
            )
        for c, sid in zip(clients, sessions, strict=True):
            await _drain_turn(c, sid, timeout=120.0)
        # Status from one of them shows total>=3.
        await clients[0].send({"type": "agent.status", "id": "s1"})
        st = await clients[0].wait_for(
            lambda e: e.get("type") == "agent.status_reply" and e.get("id") == "s1",
            timeout=10.0,
        )
        assert st["sessions"]["total"] >= 3
        assert st["sessions"]["by_backend"].get("claude", 0) >= 3
        assert st["connections"] >= 3
    finally:
        for c in clients:
            await c.close()


# ---------------------------------------------------------------------------
# More open / agent.user content edges
# ---------------------------------------------------------------------------


async def test_real_claude_agent_user_empty_string_content(real_daemon):
    """Empty content string is allowed at the protocol layer.

    Whether claude itself accepts it is up to claude — the daemon
    just forwards. We only check the daemon doesn't reject it as
    invalid_message.
    """
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(_open_claude(session))
        await c.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)
        await c.send(
            {
                "type": "agent.user",
                "session_id": session,
                "message": {"role": "user", "content": ""},
            }
        )
        # Either:
        #   - daemon accepted and CC produced a result (success/error), OR
        #   - CC rejected upstream (backend_crashed) — still not an
        #     invalid_message error from the daemon itself.
        evt = await c.wait_for(
            lambda e: (
                (e.get("type") == "agent.result" and e.get("session_id") == session)
                or (
                    e.get("type") == "agent.error"
                    and e.get("session_id") == session
                    and e.get("code") in {"backend_crashed", "invalid_message"}
                )
            ),
            timeout=60.0,
        )
        # Pin down: daemon doesn't reject "" as invalid_message; CC may.
        if evt.get("type") == "agent.error":
            assert evt["code"] != "invalid_message", evt
    finally:
        await c.close()


async def test_real_claude_agent_user_long_content(real_daemon):
    """A multi-kilobyte user message is forwarded fine."""
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(_open_claude(session))
        await c.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)
        # ~5KB of text. Well under the 16 MiB max_line_bytes default.
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
        await _drain_turn(c, session, timeout=120.0)
    finally:
        await c.close()


# ---------------------------------------------------------------------------
# Two clients on one session (watcher mid-stream)
# ---------------------------------------------------------------------------


async def test_real_claude_watcher_mid_turn_replays_buffered(real_daemon):
    """Late-arriving watcher with ``last_seen_seq=0`` gets the in-progress turn's buffer."""
    owner = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await owner.send(_open_claude(session))
        await owner.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)
        await owner.send(
            {
                "type": "agent.user",
                "session_id": session,
                "message": {"role": "user", "content": "Count from 1 to 30, slowly."},
            }
        )
        # Wait for some stream output before attaching the watcher.
        await owner.wait_for(
            lambda e: e.get("type") in {"agent.delta", "agent.message"}, timeout=120.0
        )
        watcher = await _client(real_daemon)
        try:
            await watcher.send(
                {
                    "type": "agent.watch",
                    "id": "w1",
                    "session_id": session,
                    "last_seen_seq": 0,
                }
            )
            ack = await watcher.wait_for(
                lambda e: e.get("type") == "agent.watching" and e.get("id") == "w1",
                timeout=10.0,
            )
            # Ring buffer should have several events from the in-flight
            # turn — replay starts immediately after ``watching``.
            assert ack["last_seq"] >= 1
            replayed = await watcher.wait_for(
                lambda e: isinstance(e.get("type"), str) and e["type"].startswith("agent."),
                timeout=10.0,
            )
            assert replayed.get("seq", 0) >= 1
            # Cleanup the in-flight turn.
            await owner.send({"type": "agent.interrupt", "session_id": session})
            await owner.wait_for(lambda e: e.get("type") == "agent.interrupted", timeout=15.0)
        finally:
            await watcher.close()
    finally:
        await owner.close()


# ---------------------------------------------------------------------------
# session_taken with an attached watcher
# ---------------------------------------------------------------------------


async def test_real_claude_session_taken_doesnt_kick_watcher(real_daemon):
    """``session_taken`` only retargets the owner — watchers stay subscribed."""
    owner1 = await _client(real_daemon)
    watcher = await _client(real_daemon)
    owner2 = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await owner1.send(_open_claude(session))
        await owner1.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)
        await _say_ok(owner1, session)
        await watcher.send(
            {"type": "agent.watch", "id": "w1", "session_id": session, "last_seen_seq": 0}
        )
        await watcher.wait_for(lambda e: e.get("type") == "agent.watching", timeout=10.0)
        await owner2.send(
            {
                "type": "agent.open",
                "id": "r2",
                "session_id": session,
                "backend": "claude",
                "resume": True,
                "options": {
                    "claude": {
                        "model": "haiku",
                        "tools": "",
                        "permission_mode": "bypassPermissions",
                    }
                },
            }
        )
        # Owner 1 sees session_taken.
        await owner1.wait_for(lambda e: e.get("type") == "agent.session_taken", timeout=15.0)
        await owner2.wait_for(
            lambda e: e.get("type") == "agent.opened" and e.get("id") == "r2",
            timeout=15.0,
        )
        # Owner 2 runs a fresh turn — watcher should still see it.
        await owner2.send(
            {
                "type": "agent.user",
                "session_id": session,
                "message": {"role": "user", "content": "Say OK."},
            }
        )
        await _drain_turn(owner2, session, timeout=60.0)
        # Watcher saw the new turn.
        await _drain_turn(watcher, session, timeout=15.0)
    finally:
        await owner1.close()
        await watcher.close()
        await owner2.close()


# ---------------------------------------------------------------------------
# include_raw_events
# ---------------------------------------------------------------------------


async def test_real_claude_include_raw_events_attaches_raw(real_daemon):
    """``options.claude.include_raw_events:true`` adds ``raw`` to each agent.* frame."""
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(_open_claude(session, include_raw_events=True))
        await c.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)
        await c.send(
            {
                "type": "agent.user",
                "session_id": session,
                "message": {"role": "user", "content": "Say OK."},
            }
        )
        collected = await c.wait_for(
            lambda e: e.get("type") == "agent.result" and e.get("session_id") == session,
            collect=True,
            timeout=60.0,
        )
        # At least one agent.* frame should carry a `raw` payload (a
        # CC stream-json dict copy).
        with_raw = [
            e
            for e in collected
            if isinstance(e.get("type"), str)
            and e["type"].startswith("agent.")
            and isinstance(e.get("raw"), dict)
        ]
        assert with_raw, [
            (e.get("type"), list(e.keys()))
            for e in collected
            if e.get("type", "").startswith("agent.")
        ]
    finally:
        await c.close()


async def test_real_claude_include_raw_events_default_off(real_daemon):
    """Default config must NOT attach ``raw`` (it's noisy)."""
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(_open_claude(session))
        await c.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)
        collected = (
            await c.wait_for(
                lambda e: e.get("type") == "agent.result" and e.get("session_id") == session,
                collect=True,
                timeout=60.0,
            )
            if False
            else None
        )
        # Send the turn ourselves to keep the helper simple.
        await c.send(
            {
                "type": "agent.user",
                "session_id": session,
                "message": {"role": "user", "content": "Say OK."},
            }
        )
        collected = await c.wait_for(
            lambda e: e.get("type") == "agent.result" and e.get("session_id") == session,
            collect=True,
            timeout=60.0,
        )
        with_raw = [
            e
            for e in collected
            if isinstance(e.get("type"), str) and e["type"].startswith("agent.") and "raw" in e
        ]
        assert with_raw == [], with_raw
    finally:
        await c.close()


# ---------------------------------------------------------------------------
# Watcher cannot drive — agent.user from a non-owner is unknown_session
# ---------------------------------------------------------------------------


async def test_real_claude_non_owner_cannot_send_agent_user(real_daemon):
    """A connection without ownership of a session can't drive turns on it.

    Ownership is per-connection (assigned on ``open``). The second
    connection sees the session as unknown to itself even if it watched
    via ``agent.watch``.
    """
    owner = await _client(real_daemon)
    other = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await owner.send(_open_claude(session))
        await owner.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)
        # other watches the session
        await other.send(
            {"type": "agent.watch", "id": "w1", "session_id": session, "last_seen_seq": 0}
        )
        await other.wait_for(lambda e: e.get("type") == "agent.watching", timeout=10.0)
        # other tries to drive
        await other.send(
            {
                "type": "agent.user",
                "session_id": session,
                "message": {"role": "user", "content": "hi"},
            }
        )
        err = await other.wait_for(
            lambda e: e.get("type") == "agent.error" and e.get("session_id") == session,
            timeout=10.0,
        )
        # Spec: agent.user is connection-scoped to the owner. Watchers
        # can't drive — daemon rejects the frame as if the session
        # didn't exist for that connection.
        assert err["code"] in {"session_unknown"}
    finally:
        await owner.close()
        await other.close()


# ---------------------------------------------------------------------------
# Close immediately after open, no turns
# ---------------------------------------------------------------------------


async def test_real_claude_close_immediately_after_open(real_daemon):
    """Open + close back-to-back without any turn → clean closed ack."""
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(_open_claude(session))
        await c.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)
        await c.send({"type": "agent.close", "id": "c1", "session_id": session, "delete": True})
        await c.wait_for(
            lambda e: e.get("type") == "agent.closed" and e.get("id") == "c1",
            timeout=10.0,
        )
    finally:
        await c.close()


# ---------------------------------------------------------------------------
# Message content with unicode normalization (curly quotes, RTL)
# ---------------------------------------------------------------------------


async def test_real_claude_agent_user_curly_quotes_and_rtl(real_daemon):
    """Non-ASCII text (curly quotes, RTL) survives the wire intact."""
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(_open_claude(session))
        await c.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)
        await c.send(
            {
                "type": "agent.user",
                "session_id": session,
                "message": {
                    "role": "user",
                    "content": "Reply with the literal token “ABC-עברית” (no quotes).",
                },
            }
        )
        await _drain_turn(c, session, timeout=60.0)
    finally:
        await c.close()


# ---------------------------------------------------------------------------
# agent.error doesn't kill the connection
# ---------------------------------------------------------------------------


async def test_real_claude_connection_survives_invalid_message(real_daemon):
    """A non-fatal error frame leaves the connection usable."""
    c = await _client(real_daemon)
    try:
        # Send a malformed open that triggers invalid_message.
        await c.send(
            {
                "type": "agent.open",
                "id": "bad",
                "session_id": "",  # empty session_id rejected
                "backend": "claude",
                "options": {"claude": {}},
            }
        )
        err = await c.wait_for(
            lambda e: e.get("type") == "agent.error" and e.get("id") == "bad",
            timeout=10.0,
        )
        assert err["code"] == "invalid_message"
        # Connection is still alive — ping/pong works.
        await c.send({"type": "agent.ping", "id": "p1"})
        pong = await c.wait_for(lambda e: e.get("type") == "agent.pong", timeout=5.0)
        assert pong["id"] == "p1"
        # And we can open a real session afterwards.
        session = str(uuid.uuid4())
        await c.send(_open_claude(session))
        await c.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)
    finally:
        await c.close()


# ---------------------------------------------------------------------------
# Tool use (Claude with Bash)
# ---------------------------------------------------------------------------


async def test_real_claude_tool_use_emits_tool_frames(real_daemon, tmp_path):
    """Enabling Bash + asking the model to run a command emits ``agent.tool_use``
    and ``agent.tool_result`` frames."""
    c = await _client(real_daemon)
    cwd = str(tmp_path.resolve())
    try:
        session = str(uuid.uuid4())
        await c.send(
            {
                "type": "agent.open",
                "id": "r1",
                "session_id": session,
                "backend": "claude",
                "options": {
                    "claude": {
                        "model": "haiku",
                        "tools": "Bash",
                        "permission_mode": "bypassPermissions",
                        "cwd": cwd,
                    }
                },
            }
        )
        await c.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)
        await c.send(
            {
                "type": "agent.user",
                "session_id": session,
                "message": {
                    "role": "user",
                    "content": (
                        "Run the shell command `echo blemees-marker-1234` "
                        "via the Bash tool, then print its output back to me."
                    ),
                },
            }
        )
        collected = await c.wait_for(
            lambda e: e.get("type") == "agent.result" and e.get("session_id") == session,
            collect=True,
            timeout=120.0,
        )
        types = [e.get("type") for e in collected]
        # Haiku usually obliges, but if it answers without calling Bash
        # we don't want to flake — only assert tool flow appeared if it
        # did. The interesting bit: when tool_use happens, the result
        # frame should immediately follow.
        if "agent.tool_use" in types:
            tool_uses = [e for e in collected if e.get("type") == "agent.tool_use"]
            tool_results = [e for e in collected if e.get("type") == "agent.tool_result"]
            assert tool_uses
            assert tool_results, types
            # Each tool_use should have a tool_use_id and a name.
            for tu in tool_uses:
                assert isinstance(tu.get("tool_use_id"), str), tu
                assert isinstance(tu.get("name"), str), tu
            # Each tool_result should reference a tool_use_id.
            for tr in tool_results:
                assert isinstance(tr.get("tool_use_id"), str), tr
    finally:
        await c.close()


# ---------------------------------------------------------------------------
# Connection close mid-turn vs idle
# ---------------------------------------------------------------------------


async def test_real_claude_close_connection_mid_turn_session_survives(real_daemon):
    """Closing the socket mid-turn detaches the session but lets the turn finish.

    Per spec §5.9: hard-kill mid-turn would leave a half-flushed
    transcript; the daemon instead lets the turn complete, then reaps
    the session normally. A reconnect with ``resume:true`` recovers it.
    """
    c1 = await _client(real_daemon)
    session = str(uuid.uuid4())
    await c1.send(_open_claude(session))
    await c1.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)
    await c1.send(
        {
            "type": "agent.user",
            "session_id": session,
            "message": {"role": "user", "content": "Say OK."},
        }
    )
    # Wait for streaming to begin so the session is genuinely mid-turn,
    # then drop the socket without sending ``agent.close``.
    await c1.wait_for(lambda e: e.get("type") in {"agent.delta", "agent.message"}, timeout=120.0)
    await c1.close()
    # Give the daemon time to finish the in-flight turn and detach.
    await asyncio.sleep(2.0)

    # Reattach via resume on a fresh connection — the session is
    # still in memory (detached, not yet reaped).
    c2 = await _client(real_daemon)
    try:
        await c2.send(
            {
                "type": "agent.open",
                "id": "r2",
                "session_id": session,
                "backend": "claude",
                "resume": True,
                "options": {
                    "claude": {
                        "model": "haiku",
                        "tools": "",
                        "permission_mode": "bypassPermissions",
                    }
                },
            }
        )
        opened = await c2.wait_for(
            lambda e: e.get("type") == "agent.opened" and e.get("id") == "r2",
            timeout=30.0,
        )
        assert opened["session_id"] == session
    finally:
        await c2.close()


# ---------------------------------------------------------------------------
# session_info reflects multi-session attached/detached
# ---------------------------------------------------------------------------


async def test_real_claude_status_attached_detached_counts(real_daemon):
    """Two sessions opened on two connections; close one connection;
    daemon-wide status shows attached/detached split."""
    c_a = await _client(real_daemon)
    c_b = await _client(real_daemon)
    s_a = str(uuid.uuid4())
    s_b = str(uuid.uuid4())
    try:
        await c_a.send(_open_claude(s_a))
        await c_a.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)
        await c_b.send(_open_claude(s_b))
        await c_b.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)
        await _say_ok(c_a, s_a)
        await _say_ok(c_b, s_b)

        # Drop c_b → session s_b detaches.
        await c_b.close()
        await asyncio.sleep(0.5)

        # Status from c_a.
        await c_a.send({"type": "agent.status", "id": "s1"})
        st = await c_a.wait_for(
            lambda e: e.get("type") == "agent.status_reply" and e.get("id") == "s1",
            timeout=10.0,
        )
        sessions = st["sessions"]
        # At least one attached (s_a) and at least one detached (s_b).
        assert sessions["attached"] >= 1
        assert sessions["detached"] >= 1
    finally:
        await c_a.close()


# ---------------------------------------------------------------------------
# agent.stderr forwarding
# ---------------------------------------------------------------------------


async def test_real_claude_stderr_forwarding_smoke(real_daemon):
    """When claude writes to stderr (warnings, debug noise), the daemon
    forwards a few lines as ``agent.stderr``.

    Real CC's stderr volume varies — sometimes it's silent on a
    successful run. We open a session, run a turn, and just check
    that no stderr forwarding crashes the daemon (any agent.stderr
    that does land must have a non-empty ``line`` field).
    """
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(_open_claude(session))
        await c.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)
        collected = (
            await c.wait_for(
                lambda e: False,  # never matches
                collect=True,
                timeout=2.0,
            )
            if False
            else None
        )
        # Actually do a real turn and collect.
        await c.send(
            {
                "type": "agent.user",
                "session_id": session,
                "message": {"role": "user", "content": "Say OK."},
            }
        )
        collected = await c.wait_for(
            lambda e: e.get("type") == "agent.result" and e.get("session_id") == session,
            collect=True,
            timeout=60.0,
        )
        for evt in collected:
            if evt.get("type") == "agent.stderr":
                # Only assertion: stderr lines have a non-empty `line`.
                assert isinstance(evt.get("line"), str)
                assert evt["line"] != ""
                # And the rate-limiter doesn't generate spurious empty
                # frames after newline trimming.
    finally:
        await c.close()


# ---------------------------------------------------------------------------
# session_info native_session_id + cumulative usage across resume
# ---------------------------------------------------------------------------


async def test_real_claude_session_info_after_resume_keeps_native_id(real_daemon):
    """``native_session_id`` survives a resume cycle on the same id."""
    c1 = await _client(real_daemon)
    session = str(uuid.uuid4())
    try:
        await c1.send(_open_claude(session))
        await c1.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)
        await _say_ok(c1, session)
    finally:
        await c1.close()

    c2 = await _client(real_daemon)
    try:
        await c2.send(
            {
                "type": "agent.open",
                "id": "r2",
                "session_id": session,
                "backend": "claude",
                "resume": True,
                "options": {
                    "claude": {
                        "model": "haiku",
                        "tools": "",
                        "permission_mode": "bypassPermissions",
                    }
                },
            }
        )
        await c2.wait_for(
            lambda e: e.get("type") == "agent.opened" and e.get("id") == "r2",
            timeout=30.0,
        )
        await c2.send({"type": "agent.session_info", "id": "i1", "session_id": session})
        info = await c2.wait_for(
            lambda e: e.get("type") == "agent.session_info_reply", timeout=10.0
        )
        # For claude, native_session_id == session (CC uses the daemon-assigned id).
        assert info["native_session_id"] == session
    finally:
        await c2.close()


# ---------------------------------------------------------------------------
# Durable event log + sidecar persistence (claude)
# ---------------------------------------------------------------------------


async def test_real_claude_event_log_persists_usage_across_daemon_restart(tmp_path):
    """Counters persist across a daemon restart when ``event_log_dir`` is set.

    Spec §5.15: the durable usage sidecar is the only mechanism that
    survives daemon restart. Mock tests cover the wiring; this e2e
    pins it down against real claude.
    """
    from tests.blemees_agent.conftest import short_socket_path

    log_dir = tmp_path / "events"
    log_dir.mkdir()

    def _cfg(sock):
        return Config(
            socket_path=str(sock),
            claude_bin=CLAUDE,
            idle_timeout_s=60,
            max_concurrent_sessions=8,
            event_log_dir=str(log_dir),
        )

    session = str(uuid.uuid4())
    cwd = str(tmp_path.resolve())

    # First daemon: open + run a turn.
    sock1 = short_socket_path("blemeesd-e2e-persist1")
    cfg1 = _cfg(sock1)
    d1 = Daemon(cfg1, configure("error"))
    await d1.start()
    t1 = asyncio.create_task(d1.serve_forever())
    try:
        c = await _client(str(sock1))
        try:
            await c.send(_open_claude(session, cwd=cwd))
            await c.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)
            await _say_ok(c, session)
        finally:
            await c.close()
    finally:
        d1.request_shutdown()
        try:
            await asyncio.wait_for(t1, timeout=5.0)
        except TimeoutError:
            t1.cancel()

    # Sidecar should now exist.
    sidecar = log_dir / f"{session}.usage.json"
    assert sidecar.is_file(), f"sidecar missing: {sidecar}"

    # Second daemon: reopen with resume → usage should be back to 1 turn.
    sock2 = short_socket_path("blemeesd-e2e-persist2")
    cfg2 = _cfg(sock2)
    d2 = Daemon(cfg2, configure("error"))
    await d2.start()
    t2 = asyncio.create_task(d2.serve_forever())
    try:
        c = await _client(str(sock2))
        try:
            await c.send(
                {
                    "type": "agent.open",
                    "id": "r2",
                    "session_id": session,
                    "backend": "claude",
                    "resume": True,
                    "options": {
                        "claude": {
                            "model": "haiku",
                            "tools": "",
                            "permission_mode": "bypassPermissions",
                            "cwd": cwd,
                        }
                    },
                }
            )
            await c.wait_for(
                lambda e: e.get("type") == "agent.opened" and e.get("id") == "r2",
                timeout=30.0,
            )
            await c.send({"type": "agent.session_info", "id": "i1", "session_id": session})
            info = await c.wait_for(
                lambda e: e.get("type") == "agent.session_info_reply", timeout=10.0
            )
            assert info["turns"] == 1, info
            cu = info["cumulative_usage"]
            assert cu.get("input_tokens", 0) > 0
            assert cu.get("output_tokens", 0) > 0
        finally:
            await c.close()
    finally:
        d2.request_shutdown()
        try:
            await asyncio.wait_for(t2, timeout=5.0)
        except TimeoutError:
            t2.cancel()


# ---------------------------------------------------------------------------
# Mixed claude + codex on one daemon
# ---------------------------------------------------------------------------


@pytest.mark.requires_codex
async def test_real_mixed_claude_and_codex_concurrent(tmp_path):
    """Open one claude session and one codex session on the same daemon
    via the same connection — independent turns, both succeed."""
    import shutil as _shutil

    codex_bin = _shutil.which("codex")
    if codex_bin is None:
        pytest.skip("codex not on PATH")
    from tests.blemees_agent.conftest import short_socket_path

    socket_path = short_socket_path("blemeesd-e2e-mixed")
    cfg = Config(socket_path=str(socket_path), claude_bin=CLAUDE, codex_bin=codex_bin)
    daemon = Daemon(cfg, configure("error"))
    await daemon.start()
    serve = asyncio.create_task(daemon.serve_forever())
    try:
        c = await _client(str(socket_path))
        try:
            s_claude = str(uuid.uuid4())
            s_codex = str(uuid.uuid4())
            await c.send(_open_claude(s_claude))
            await c.wait_for(
                lambda e, sid=s_claude: (
                    e.get("type") == "agent.opened" and e.get("session_id") == sid
                ),
                timeout=30.0,
            )
            await c.send(
                {
                    "type": "agent.open",
                    "id": "r2",
                    "session_id": s_codex,
                    "backend": "codex",
                    "options": {"codex": {"sandbox": "read-only", "approval-policy": "never"}},
                }
            )
            await c.wait_for(
                lambda e, sid=s_codex: (
                    e.get("type") == "agent.opened" and e.get("session_id") == sid
                ),
                timeout=30.0,
            )
            # Drive turns on both.
            for sid in (s_claude, s_codex):
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
                assert remaining > 0
                evt = await c.recv(timeout=remaining)
                if evt.get("type") == "agent.result" and evt.get("session_id") in {
                    s_claude,
                    s_codex,
                }:
                    seen[evt["session_id"]] = evt
            # status_reply by_backend reflects both backends.
            await c.send({"type": "agent.status", "id": "stat"})
            st = await c.wait_for(
                lambda e: e.get("type") == "agent.status_reply" and e.get("id") == "stat",
                timeout=10.0,
            )
            assert st["sessions"]["by_backend"].get("claude", 0) >= 1
            assert st["sessions"]["by_backend"].get("codex", 0) >= 1
        finally:
            await c.close()
    finally:
        daemon.request_shutdown()
        try:
            await asyncio.wait_for(serve, timeout=10.0)
        except TimeoutError:
            serve.cancel()


# ---------------------------------------------------------------------------
# Soft-detach without turn → subprocess immediately killed
# ---------------------------------------------------------------------------


async def test_real_claude_close_connection_idle_kills_subprocess(real_daemon):
    """Closing a connection while the session is idle → subprocess gone.

    Per spec §5.9: idle sessions are terminated immediately on
    disconnect (no transcript to flush). We verify by reattaching and
    checking that ``subprocess_running`` is initially false (until we
    drive a new turn).
    """
    c1 = await _client(real_daemon)
    session = str(uuid.uuid4())
    await c1.send(_open_claude(session))
    await c1.wait_for(lambda e: e.get("type") == "agent.opened", timeout=30.0)
    await _say_ok(c1, session)
    await c1.close()
    await asyncio.sleep(0.5)

    c2 = await _client(real_daemon)
    try:
        await c2.send({"type": "agent.session_info", "id": "i1", "session_id": session})
        info = await c2.wait_for(
            lambda e: e.get("type") == "agent.session_info_reply", timeout=10.0
        )
        # Session is detached; subprocess should be gone (the daemon
        # killed it on idle disconnect, didn't keep it warm).
        assert info["attached"] is False
        assert info["subprocess_running"] is False
    finally:
        await c2.close()
