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

from blemees import PROTOCOL_VERSION
from blemees.config import Config
from blemees.daemon import Daemon
from blemees.logging import configure

CLAUDE = shutil.which("claude")


pytestmark = pytest.mark.requires_claude


def _need_claude() -> None:
    if CLAUDE is None:
        pytest.skip("`claude` not on PATH", allow_module_level=True)


_need_claude()


@pytest_asyncio.fixture
async def real_daemon(tmp_path):
    from tests.blemees.conftest import short_socket_path

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
    from tests.blemees.conftest import _StreamClient  # reuse helper

    reader, writer = await asyncio.open_unix_connection(socket_path)
    c = _StreamClient(reader, writer)
    await c.send({"type": "blemeesd.hello", "client": "e2e/0", "protocol": PROTOCOL_VERSION})
    ack = await c.recv()
    assert ack["type"] == "blemeesd.hello_ack"
    return c


async def test_real_claude_turn_produces_result(real_daemon):
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(
            {
                "type": "blemeesd.open",
                "id": "r1",
                "session_id": session,
                "model": "haiku",
                "tools": "",
                "permission_mode": "bypassPermissions",
            }
        )
        await c.wait_for(lambda e: e.get("type") == "blemeesd.opened", timeout=30.0)
        await c.send(
            {
                "type": "claude.user",
                "session_id": session,
                "message": {"role": "user", "content": "Say OK."},
            }
        )
        res = await c.wait_for(
            lambda e: e.get("type") == "claude.result" and e.get("session_id") == session,
            timeout=60.0,
        )
        assert res["subtype"] in {"success", "error_max_turns", "error_during_execution"}
    finally:
        await c.close()


async def test_real_claude_resume_preserves_context(real_daemon):
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(
            {
                "type": "blemeesd.open",
                "id": "r1",
                "session_id": session,
                "model": "haiku",
                "tools": "",
                "permission_mode": "bypassPermissions",
            }
        )
        await c.wait_for(lambda e: e.get("type") == "blemeesd.opened", timeout=30.0)
        await c.send(
            {
                "type": "claude.user",
                "session_id": session,
                "message": {"role": "user", "content": "Remember the number 17."},
            }
        )
        await c.wait_for(
            lambda e: e.get("type") == "claude.result" and e.get("session_id") == session,
            timeout=60.0,
        )
        await c.send(
            {
                "type": "claude.user",
                "session_id": session,
                "message": {
                    "role": "user",
                    "content": "What number did I ask you to remember? Answer with just the number.",
                },
            }
        )
        collected = await c.wait_for(
            lambda e: e.get("type") == "claude.result" and e.get("session_id") == session,
            collect=True,
            timeout=60.0,
        )
        # Concatenate any text from assistant messages seen.
        text = ""
        for evt in collected:
            if evt.get("type") == "claude.assistant":
                for block in evt.get("message", {}).get("content", []):
                    if isinstance(block, dict) and block.get("type") == "text":
                        text += block.get("text", "")
        assert "17" in text
    finally:
        await c.close()


async def test_real_claude_interrupt_then_continue(real_daemon):
    c = await _client(real_daemon)
    try:
        session = str(uuid.uuid4())
        await c.send(
            {
                "type": "blemeesd.open",
                "id": "r1",
                "session_id": session,
                "model": "haiku",
                "tools": "",
                "permission_mode": "bypassPermissions",
            }
        )
        await c.wait_for(lambda e: e.get("type") == "blemeesd.opened", timeout=30.0)
        await c.send(
            {
                "type": "claude.user",
                "session_id": session,
                "message": {
                    "role": "user",
                    "content": "Count slowly from 1 to 200, one number per line.",
                },
            }
        )
        await c.wait_for(lambda e: e.get("type") == "claude.stream_event", timeout=60.0)
        await c.send({"type": "blemeesd.interrupt", "session_id": session})
        await c.wait_for(lambda e: e.get("type") == "blemeesd.interrupted", timeout=10.0)
        # Subsequent turn still works.
        await c.send(
            {
                "type": "claude.user",
                "session_id": session,
                "message": {"role": "user", "content": "Say OK."},
            }
        )
        await c.wait_for(
            lambda e: e.get("type") == "claude.result" and e.get("session_id") == session,
            timeout=60.0,
        )
    finally:
        await c.close()
