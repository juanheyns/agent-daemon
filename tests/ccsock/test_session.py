"""Unit tests for the SessionTable. No claude subprocess involved."""

from __future__ import annotations

import asyncio

import pytest

from ccsock.errors import SessionExistsError, SessionUnknownError
from ccsock.protocol import OpenMessage
from ccsock.session import Session, SessionTable


def _open_msg(session: str = "s1") -> OpenMessage:
    return OpenMessage(id=None, session=session, resume=False, fields={"session": session})


async def test_register_and_get():
    table = SessionTable(idle_timeout_s=60, max_concurrent=8)
    sess = Session(session_id="s1", open_msg=_open_msg(), cwd=None)
    await table.register(sess)
    assert table.get("s1") is sess


async def test_register_duplicate_raises():
    table = SessionTable(idle_timeout_s=60, max_concurrent=8)
    await table.register(Session(session_id="s1", open_msg=_open_msg(), cwd=None))
    with pytest.raises(SessionExistsError):
        await table.register(Session(session_id="s1", open_msg=_open_msg(), cwd=None))


async def test_get_unknown_raises():
    table = SessionTable(idle_timeout_s=60, max_concurrent=8)
    with pytest.raises(SessionUnknownError):
        table.get("missing")


async def test_detach_marks_idle_time():
    table = SessionTable(idle_timeout_s=60, max_concurrent=8)
    sess = Session(session_id="s1", open_msg=_open_msg(), cwd=None, connection_id=42)
    await table.register(sess)
    await table.detach("s1")
    again = table.get("s1")
    assert again.connection_id is None
    assert again.detached_at is not None


async def test_reap_idle_removes_expired_only():
    table = SessionTable(idle_timeout_s=1.0, max_concurrent=8)
    a = Session(session_id="a", open_msg=_open_msg("a"), cwd=None, connection_id=1)
    b = Session(session_id="b", open_msg=_open_msg("b"), cwd=None, connection_id=2)
    await table.register(a)
    await table.register(b)
    await table.detach("a")
    # Backdate detachment so the reaper considers it expired.
    import time
    table.get("a").detached_at = time.monotonic() - 2.0
    expired = await table.reap_idle()
    assert expired == ["a"]
    assert table.try_get("a") is None
    assert table.try_get("b") is not None  # still attached


async def test_detach_all_for_connection():
    table = SessionTable(idle_timeout_s=60, max_concurrent=8)
    await table.register(Session(session_id="a", open_msg=_open_msg("a"), cwd=None, connection_id=1))
    await table.register(Session(session_id="b", open_msg=_open_msg("b"), cwd=None, connection_id=1))
    await table.register(Session(session_id="c", open_msg=_open_msg("c"), cwd=None, connection_id=2))
    detached = await table.detach_all_for_connection(1)
    assert set(detached) == {"a", "b"}
    assert table.get("a").connection_id is None
    assert table.get("b").connection_id is None
    assert table.get("c").connection_id == 2


async def test_reattach_clears_idle_time():
    table = SessionTable(idle_timeout_s=60, max_concurrent=8)
    await table.register(Session(session_id="s1", open_msg=_open_msg(), cwd=None, connection_id=1))
    await table.detach("s1")
    sess = await table.attach_existing("s1", connection_id=7)
    assert sess.connection_id == 7
    assert sess.detached_at is None


async def test_max_concurrent_enforced():
    table = SessionTable(idle_timeout_s=60, max_concurrent=1)
    await table.register(Session(session_id="a", open_msg=_open_msg("a"), cwd=None))
    with pytest.raises(SessionExistsError):
        await table.register(Session(session_id="b", open_msg=_open_msg("b"), cwd=None))


async def test_remove_is_idempotent():
    table = SessionTable(idle_timeout_s=60, max_concurrent=8)
    await table.register(Session(session_id="s1", open_msg=_open_msg(), cwd=None))
    await table.remove("s1")
    await table.remove("s1")  # should not raise
    assert table.try_get("s1") is None
