"""Unit tests for blemees.protocol."""

from __future__ import annotations

import json

import pytest

from blemees import PROTOCOL_VERSION
from blemees.errors import (
    OversizeMessageError,
    ProtocolError,
    UnsafeFlagError,
)
from blemees.protocol import (
    OpenMessage,
    build_claude_argv,
    build_user_stdin_line,
    encode,
    error_frame,
    hello_ack,
    parse_close,
    parse_hello,
    parse_interrupt,
    parse_line,
    parse_list_sessions,
    parse_open,
    parse_user,
)


# ---------------------------------------------------------------------------
# Framing / encode / decode
# ---------------------------------------------------------------------------

def test_encode_is_newline_terminated_utf8():
    data = encode({"type": "blemeesd.hello", "emoji": "🌟"})
    assert data.endswith(b"\n")
    assert b"\n" not in data[:-1]
    decoded = json.loads(data)
    assert decoded["emoji"] == "🌟"


def test_parse_line_accepts_valid_object():
    obj = parse_line(b'{"type":"blemeesd.hello","protocol":"blemees/1"}\n')
    assert obj["type"] == "blemeesd.hello"


def test_parse_line_rejects_non_object():
    with pytest.raises(ProtocolError):
        parse_line(b"[]\n")


def test_parse_line_rejects_missing_type():
    with pytest.raises(ProtocolError):
        parse_line(b'{"foo":"bar"}\n')


def test_parse_line_rejects_empty():
    with pytest.raises(ProtocolError):
        parse_line(b"\n")


def test_parse_line_rejects_malformed_json():
    with pytest.raises(ProtocolError):
        parse_line(b"not-json\n")


def test_parse_line_rejects_oversize():
    with pytest.raises(OversizeMessageError):
        parse_line(b"x" * 100, max_bytes=50)


def test_parse_line_handles_surrogate_pairs():
    # Emoji encoded as two UTF-16 surrogates must round-trip via JSON.
    raw = json.dumps({"type": "x", "text": "\U0001F600"}).encode("utf-8") + b"\n"
    obj = parse_line(raw)
    assert obj["text"] == "\U0001F600"


def test_parse_line_allows_embedded_nul_in_json_string():
    payload = json.dumps({"type": "x", "text": "a\x00b"}).encode("utf-8") + b"\n"
    obj = parse_line(payload)
    assert obj["text"] == "a\x00b"


# ---------------------------------------------------------------------------
# hello / hello_ack
# ---------------------------------------------------------------------------

def test_parse_hello_requires_protocol():
    with pytest.raises(ProtocolError):
        parse_hello({"type": "blemeesd.hello"})


def test_parse_hello_ok():
    h = parse_hello({"type": "blemeesd.hello", "protocol": "blemees/1", "client": "t/0.1"})
    assert h.protocol == "blemees/1"
    assert h.client == "t/0.1"


def test_hello_ack_shape():
    ack = hello_ack("0.1.0", 1234, "2.1.118")
    assert ack["type"] == "blemeesd.hello_ack"
    assert ack["daemon"] == "blemeesd/0.1.0"
    assert ack["protocol"] == PROTOCOL_VERSION
    assert ack["pid"] == 1234
    assert ack["claude_version"] == "2.1.118"


# ---------------------------------------------------------------------------
# open
# ---------------------------------------------------------------------------

def test_parse_open_requires_session():
    with pytest.raises(ProtocolError):
        parse_open({"type": "blemeesd.open"})


def test_parse_open_rejects_unsafe_flag_field():
    with pytest.raises(UnsafeFlagError):
        parse_open(
            {
                "type": "blemeesd.open",
                "session": "s1",
                "dangerously_skip_permissions": True,
            }
        )


def test_parse_open_rejects_unsafe_flag_literal_in_values():
    with pytest.raises(UnsafeFlagError):
        parse_open(
            {
                "type": "blemeesd.open",
                "session": "s1",
                "disallowed_tools": ["--dangerously-skip-permissions"],
            }
        )


def test_parse_open_rejects_client_set_input_format():
    with pytest.raises(ProtocolError) as exc:
        parse_open(
            {
                "type": "blemeesd.open",
                "session": "s1",
                "input_format": "text",
            }
        )
    assert "input_format" in str(exc.value)


def test_parse_open_rejects_client_set_output_format():
    with pytest.raises(ProtocolError) as exc:
        parse_open(
            {
                "type": "blemeesd.open",
                "session": "s1",
                "output_format": "json",
            }
        )
    assert "output_format" in str(exc.value)


def test_parse_open_allows_bypass_permissions_mode():
    # Explicitly allowed by spec §5.4.
    msg = parse_open(
        {
            "type": "blemeesd.open",
            "session": "s1",
            "permission_mode": "bypassPermissions",
        }
    )
    assert msg.fields["permission_mode"] == "bypassPermissions"


def test_build_argv_default_flags_and_session_id():
    msg = OpenMessage(id=None, session="s1", resume=False, fields={"session": "s1"})
    argv = build_claude_argv("claude", msg)
    assert argv[:3] == ["claude", "-p", "--verbose"]
    assert "--session-id" in argv
    assert argv[argv.index("--session-id") + 1] == "s1"
    assert "--input-format" in argv
    assert "--output-format" in argv


def test_build_argv_resume_replaces_session_id():
    msg = OpenMessage(id=None, session="s1", resume=True, fields={"session": "s1"})
    argv = build_claude_argv("claude", msg)
    assert "--resume" in argv
    assert argv[argv.index("--resume") + 1] == "s1"
    assert "--session-id" not in argv


def test_build_argv_for_resume_flag_forces_resume():
    msg = OpenMessage(id=None, session="s1", resume=False, fields={"session": "s1"})
    argv = build_claude_argv("claude", msg, for_resume=True)
    assert "--resume" in argv
    assert "--session-id" not in argv


def test_build_argv_tools_empty_string_disables_all():
    msg = OpenMessage(
        id=None, session="s1", resume=False, fields={"session": "s1", "tools": ""}
    )
    argv = build_claude_argv("claude", msg)
    i = argv.index("--tools")
    assert argv[i + 1] == ""


def test_build_argv_maps_many_fields():
    fields = {
        "session": "s1",
        "model": "sonnet",
        "system_prompt": "sp",
        "append_system_prompt": "asp",
        "tools": "default",
        "disallowed_tools": ["A", "B"],
        "permission_mode": "default",
        "add_dir": ["/a", "/b"],
        "effort": "medium",
        "agent": "dev",
        "agents": {"dev": {}},
        "mcp_config": ["m.json"],
        "strict_mcp_config": True,
        "settings": "/s.json",
        "setting_sources": "user",
        "plugin_dir": ["/p1", "/p2"],
        "betas": ["b1"],
        "exclude_dynamic_system_prompt_sections": True,
        "max_budget_usd": 1.25,
        "json_schema": {"type": "object"},
        "fallback_model": "haiku",
        "session_name": "pretty",
        "session_persistence": False,
        "include_partial_messages": True,
        "replay_user_messages": True,
    }
    msg = OpenMessage(id=None, session="s1", resume=False, fields=fields)
    argv = build_claude_argv("claude", msg)
    joined = " ".join(argv)
    assert "--model sonnet" in joined
    assert "--system-prompt sp" in joined
    assert "--append-system-prompt asp" in joined
    assert "--disallowedTools A B" in joined
    assert "--permission-mode default" in joined
    assert "--add-dir /a /b" in joined
    assert "--effort medium" in joined
    assert "--agent dev" in joined
    assert "--agents" in joined
    assert "--mcp-config m.json" in joined
    assert "--strict-mcp-config" in joined
    assert "--settings /s.json" in joined
    assert "--setting-sources user" in joined
    assert argv.count("--plugin-dir") == 2
    assert "--betas b1" in joined
    assert "--exclude-dynamic-system-prompt-sections" in joined
    assert "--max-budget-usd 1.25" in joined
    assert "--json-schema" in joined
    assert "--fallback-model haiku" in joined
    assert "-n pretty" in joined
    assert "--no-session-persistence" in joined
    assert "--include-partial-messages" in joined
    assert "--replay-user-messages" in joined


def test_build_argv_unset_fields_omit_flags():
    msg = OpenMessage(id=None, session="s1", resume=False, fields={"session": "s1"})
    argv = build_claude_argv("claude", msg)
    assert "--model" not in argv
    assert "--system-prompt" not in argv
    assert "--tools" not in argv


# ---------------------------------------------------------------------------
# user / interrupt / close
# ---------------------------------------------------------------------------

def test_parse_user_text():
    u = parse_user({"type": "claude.user", "session": "s1", "text": "hello"})
    assert u.text == "hello"
    assert u.content is None


def test_parse_user_content():
    u = parse_user(
        {
            "type": "claude.user",
            "session": "s1",
            "content": [{"type": "text", "text": "hi"}],
        }
    )
    assert u.content == [{"type": "text", "text": "hi"}]


def test_parse_user_requires_text_or_content():
    with pytest.raises(ProtocolError):
        parse_user({"type": "claude.user", "session": "s1"})


def test_build_user_stdin_line_text():
    line = build_user_stdin_line("s1", text="hi", content=None)
    obj = json.loads(line)
    assert obj == {
        "type": "user",
        "message": {"role": "user", "content": "hi"},
        "session_id": "s1",
    }
    assert line.endswith(b"\n")


def test_build_user_stdin_line_content_wins_over_text():
    line = build_user_stdin_line(
        "s1", text="ignored", content=[{"type": "text", "text": "hi"}]
    )
    obj = json.loads(line)
    assert obj["message"]["content"] == [{"type": "text", "text": "hi"}]


def test_parse_interrupt_requires_session():
    with pytest.raises(ProtocolError):
        parse_interrupt({"type": "blemeesd.interrupt"})


def test_parse_close_defaults_delete_false():
    c = parse_close({"type": "blemeesd.close", "session": "s1"})
    assert c.delete is False


def test_parse_close_delete_true():
    c = parse_close({"type": "blemeesd.close", "session": "s1", "delete": True})
    assert c.delete is True


# ---------------------------------------------------------------------------
# list_sessions
# ---------------------------------------------------------------------------

def test_parse_list_sessions_requires_cwd():
    with pytest.raises(ProtocolError):
        parse_list_sessions({"type": "blemeesd.list_sessions"})


def test_parse_list_sessions_rejects_non_string_cwd():
    with pytest.raises(ProtocolError):
        parse_list_sessions({"type": "blemeesd.list_sessions", "cwd": 42})


def test_parse_list_sessions_ok():
    msg = parse_list_sessions(
        {"type": "blemeesd.list_sessions", "id": "r1", "cwd": "/home/u/proj"}
    )
    assert msg.cwd == "/home/u/proj"
    assert msg.id == "r1"


# ---------------------------------------------------------------------------
# error_frame
# ---------------------------------------------------------------------------

def test_error_frame_includes_optional_fields():
    frame = error_frame("invalid_message", "oops", id="req_1", session="s1")
    assert frame["code"] == "invalid_message"
    assert frame["id"] == "req_1"
    assert frame["session"] == "s1"


def test_error_frame_omits_unset_ids():
    frame = error_frame("internal", "bad")
    assert "id" not in frame
    assert "session" not in frame
