"""Tests for the interactive wire-protocol tester (`blemeesctl` CLI).

We don't drive the actual REPL loop here — the end-to-end value of
that lives on top of the daemon e2e suite. These tests cover the
pieces that benefit from being verified in isolation: the field
coercion and the command dispatcher (using a recording harness so we
can assert the exact wire frame each command produces).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from blemees_agent.cli import Harness, dispatch, parse_fields

# ---- field coercion --------------------------------------------------


def test_parse_fields_coerces_primitives():
    out = parse_fields(["a=1", "b=true", "c=false", "d=hello", "e=null"])
    assert out == {"a": 1, "b": True, "c": False, "d": "hello", "e": None}


def test_parse_fields_negative_int():
    assert parse_fields(["last_seen_seq=-1"]) == {"last_seen_seq": -1}


def test_parse_fields_inline_json():
    out = parse_fields(['allowlist=["Bash","Read"]', 'options={"x":1}'])
    assert out == {"allowlist": ["Bash", "Read"], "options": {"x": 1}}


def test_parse_fields_string_with_dashes_passes_through():
    # bypassPermissions, sonnet, etc. — not coerced as JSON, kept as strings.
    assert parse_fields(["model=sonnet", "permission_mode=bypassPermissions"]) == {
        "model": "sonnet",
        "permission_mode": "bypassPermissions",
    }


def test_parse_fields_rejects_token_without_equals():
    with pytest.raises(ValueError):
        parse_fields(["bare-token"])


# ---- dispatcher: each command produces the right wire frame ---------


class RecordingHarness(Harness):
    """Captures outbound frames in-memory so dispatch() can be black-box tested."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[dict[str, Any]] = []
        self.notes: list[str] = []

    async def _send(self, frame):
        self.sent.append(frame)

    async def _print_note(self, msg):
        self.notes.append(msg)

    async def connect(self, path):
        # Don't actually open a socket; just record the hello as if we had.
        await self.hello()


@pytest.fixture
def h() -> RecordingHarness:
    return RecordingHarness()


async def test_hello_command(h):
    assert await dispatch(h, "hello") is True
    assert h.sent[-1]["type"] == "agent.hello"
    assert h.sent[-1]["protocol"]


async def test_ping_status_emit_request_id(h):
    await dispatch(h, "ping")
    await dispatch(h, "status")
    assert h.sent[0]["type"] == "agent.ping" and h.sent[0]["id"].startswith("req_")
    assert h.sent[1]["type"] == "agent.status" and h.sent[1]["id"].startswith("req_")


async def test_open_with_kv_fields(h):
    await dispatch(h, "open my-session model=sonnet permission_mode=bypassPermissions")
    f = h.sent[-1]
    assert f["type"] == "agent.open"
    assert f["session_id"] == "my-session"
    assert f["backend"] == "claude"
    assert f["options"]["claude"]["model"] == "sonnet"
    assert f["options"]["claude"]["permission_mode"] == "bypassPermissions"


async def test_open_with_explicit_backend(h):
    await dispatch(h, "open codex-session backend=codex model=gpt-5.2-codex sandbox=read-only")
    f = h.sent[-1]
    assert f["backend"] == "codex"
    assert f["options"]["codex"]["model"] == "gpt-5.2-codex"
    assert f["options"]["codex"]["sandbox"] == "read-only"


async def test_open_new_generates_uuid_and_notes_it(h):
    await dispatch(h, "open new")
    assert h.sent[-1]["type"] == "agent.open"
    sid = h.sent[-1]["session_id"]
    assert len(sid) == 36 and sid.count("-") == 4  # uuid4 shape
    assert any(sid in note for note in h.notes)


async def test_resume_sets_resume_true(h):
    await dispatch(h, "resume my-session last_seen_seq=42")
    f = h.sent[-1]
    assert f["resume"] is True
    assert f["last_seen_seq"] == 42


async def test_close_with_delete_flag(h):
    await dispatch(h, "close my-session --delete")
    assert h.sent[-1] == {"type": "agent.close", "session_id": "my-session", "delete": True}


async def test_close_without_delete(h):
    await dispatch(h, "close my-session")
    assert h.sent[-1]["delete"] is False


async def test_interrupt(h):
    await dispatch(h, "interrupt my-session")
    assert h.sent[-1] == {"type": "agent.interrupt", "session_id": "my-session"}


async def test_send_text_wraps_in_user_message(h):
    await dispatch(h, "send my-session hello there")
    assert h.sent[-1] == {
        "type": "agent.user",
        "session_id": "my-session",
        "message": {"role": "user", "content": "hello there"},
    }


async def test_send_json_uses_raw_message(h):
    await dispatch(
        h, 'send-json my-session {"role":"user","content":[{"type":"text","text":"hi"}]}'
    )
    f = h.sent[-1]
    assert f["type"] == "agent.user"
    assert f["message"]["content"][0] == {"type": "text", "text": "hi"}


async def test_raw_passes_through_arbitrary_json(h):
    await dispatch(h, 'raw {"type":"agent.future_verb","custom":1}')
    assert h.sent[-1] == {"type": "agent.future_verb", "custom": 1}


async def test_raw_with_invalid_json_does_not_crash_repl(h, capsys):
    # dispatch returns True (keep-going) and prints an error rather than throwing.
    assert await dispatch(h, "raw not-json") is True
    assert h.sent == []
    out = capsys.readouterr().out
    assert "invalid JSON" in out


async def test_quit_returns_false(h):
    assert await dispatch(h, "quit") is False
    assert await dispatch(h, "exit") is False
    assert await dispatch(h, ".q") is False


async def test_help_prints_and_continues(h, capsys):
    assert await dispatch(h, "help") is True
    out = capsys.readouterr().out
    assert "Commands" in out


async def test_unknown_command_does_not_send(h, capsys):
    assert await dispatch(h, "frobulate") is True
    assert h.sent == []
    assert "unknown command" in capsys.readouterr().out


async def test_blank_line_and_comment_are_noops(h):
    await dispatch(h, "")
    await dispatch(h, "   ")
    await dispatch(h, "# this is a comment")
    assert h.sent == []


async def test_watch_unwatch(h):
    await dispatch(h, "watch my-session last_seen_seq=10")
    await dispatch(h, "unwatch my-session")
    assert h.sent[0]["type"] == "agent.watch"
    assert h.sent[0]["last_seen_seq"] == 10
    assert h.sent[1]["type"] == "agent.unwatch"


async def test_pretty_and_quiet_toggles(h):
    assert h.pretty is False
    assert h.quiet is False
    await dispatch(h, "pretty on")
    assert h.pretty is True
    await dispatch(h, "pretty off")
    assert h.pretty is False
    await dispatch(h, "quiet on")
    assert h.quiet is True


# Sanity that a frame round-trips JSON cleanly when sent through the
# recording harness — guards against accidental introduction of
# non-serializable objects in the dispatcher.
async def test_all_recorded_frames_are_json_serializable(h):
    await dispatch(h, "open new model=sonnet")
    await dispatch(h, "send new hi")
    await dispatch(h, 'raw {"type":"x","arr":[1,2,3]}')
    for frame in h.sent:
        json.dumps(frame)
