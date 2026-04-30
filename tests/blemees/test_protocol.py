"""Unit tests for blemees.protocol (blemees/2)."""

from __future__ import annotations

import json

import pytest

from blemees import PROTOCOL_VERSION
from blemees.backends.claude import (
    build_argv as build_claude_argv,
    build_user_stdin_line,
    validate_options as validate_claude_options,
)
from blemees.errors import (
    OversizeMessageError,
    ProtocolError,
    UnknownBackendError,
    UnsafeFlagError,
)
from blemees.protocol import (
    encode,
    error_frame,
    hello_ack,
    parse_close,
    parse_hello,
    parse_interrupt,
    parse_line,
    parse_list_sessions,
    parse_open,
    parse_ping,
    parse_session_info,
    parse_status,
    parse_unwatch,
    parse_user,
    parse_watch,
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
    obj = parse_line(b'{"type":"blemeesd.hello","protocol":"blemees/2"}\n')
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
    raw = json.dumps({"type": "x", "text": "\U0001f600"}).encode("utf-8") + b"\n"
    obj = parse_line(raw)
    assert obj["text"] == "\U0001f600"


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
    h = parse_hello({"type": "blemeesd.hello", "protocol": "blemees/2", "client": "t/0.1"})
    assert h.protocol == "blemees/2"
    assert h.client == "t/0.1"


def test_hello_ack_shape():
    ack = hello_ack("0.1.0", 1234, {"claude": "2.1.118", "codex": "0.125.0"})
    assert ack["type"] == "blemeesd.hello_ack"
    assert ack["daemon"] == "blemeesd/0.1.0"
    assert ack["protocol"] == PROTOCOL_VERSION
    assert ack["pid"] == 1234
    assert ack["backends"] == {"claude": "2.1.118", "codex": "0.125.0"}


# ---------------------------------------------------------------------------
# open
# ---------------------------------------------------------------------------


def _open_frame(**overrides):
    """Helper for the canonical claude open frame."""
    base = {
        "type": "blemeesd.open",
        "session_id": "s1",
        "backend": "claude",
        "options": {"claude": {}},
    }
    base.update(overrides)
    return base


def test_parse_open_minimal_claude():
    msg = parse_open(_open_frame())
    assert msg.session_id == "s1"
    assert msg.backend == "claude"
    assert msg.options == {}
    assert msg.resume is False


def test_parse_open_requires_session():
    with pytest.raises(ProtocolError):
        parse_open({"type": "blemeesd.open", "backend": "claude", "options": {"claude": {}}})


def test_parse_open_requires_backend():
    with pytest.raises(ProtocolError):
        parse_open({"type": "blemeesd.open", "session_id": "s1", "options": {}})


def test_parse_open_rejects_unknown_backend():
    with pytest.raises(UnknownBackendError):
        parse_open(
            {
                "type": "blemeesd.open",
                "session_id": "s1",
                "backend": "anthropic",
                "options": {},
            }
        )


def test_parse_open_rejects_top_level_legacy_field():
    with pytest.raises(ProtocolError):
        parse_open(_open_frame(model="sonnet"))


def test_parse_open_resume_flag():
    msg = parse_open(_open_frame(resume=True))
    assert msg.resume is True


def test_parse_open_last_seen_seq():
    msg = parse_open(_open_frame(last_seen_seq=42))
    assert msg.last_seen_seq == 42


def test_parse_open_rejects_negative_last_seen_seq():
    with pytest.raises(ProtocolError):
        parse_open(_open_frame(last_seen_seq=-1))


def test_parse_open_rejects_sibling_options_block():
    with pytest.raises(ProtocolError):
        parse_open(
            {
                "type": "blemeesd.open",
                "session_id": "s1",
                "backend": "claude",
                "options": {"claude": {}, "anthropic": {}},
            }
        )


def test_parse_open_extracts_chosen_backend_options():
    msg = parse_open(
        {
            "type": "blemeesd.open",
            "session_id": "s1",
            "backend": "claude",
            "options": {"claude": {"model": "sonnet", "tools": ""}},
        }
    )
    assert msg.options == {"model": "sonnet", "tools": ""}


# ---------------------------------------------------------------------------
# options.claude validation (lives in backends/claude.py now, but the
# daemon's open path runs it before spawn — so it's part of the wire
# contract).
# ---------------------------------------------------------------------------


def test_validate_claude_options_rejects_unsafe_flag_field():
    with pytest.raises(UnsafeFlagError):
        validate_claude_options({"dangerously_skip_permissions": True})


def test_validate_claude_options_rejects_unsafe_flag_literal_in_values():
    with pytest.raises(UnsafeFlagError):
        validate_claude_options({"disallowed_tools": ["--dangerously-skip-permissions"]})


def test_validate_claude_options_rejects_input_format():
    with pytest.raises(ProtocolError) as exc:
        validate_claude_options({"input_format": "text"})
    assert "input_format" in str(exc.value)


def test_validate_claude_options_rejects_output_format():
    with pytest.raises(ProtocolError) as exc:
        validate_claude_options({"output_format": "json"})
    assert "output_format" in str(exc.value)


def test_validate_claude_options_allows_bypass_permissions_mode():
    validate_claude_options({"permission_mode": "bypassPermissions"})


def test_validate_claude_options_rejects_unknown_key():
    with pytest.raises(ProtocolError):
        validate_claude_options({"not_a_real_field": True})


# ---------------------------------------------------------------------------
# build_argv
# ---------------------------------------------------------------------------


def test_build_argv_default_flags_and_session_id():
    argv = build_claude_argv("claude", session_id="s1", options={}, for_resume=False)
    assert argv[:3] == ["claude", "-p", "--verbose"]
    assert "--session-id" in argv
    assert argv[argv.index("--session-id") + 1] == "s1"
    assert "--input-format" in argv
    assert "--output-format" in argv


def test_build_argv_resume_replaces_session_id():
    argv = build_claude_argv("claude", session_id="s1", options={}, for_resume=True)
    assert "--resume" in argv
    assert argv[argv.index("--resume") + 1] == "s1"
    assert "--session-id" not in argv


def test_build_argv_tools_empty_string_disables_all():
    argv = build_claude_argv("claude", session_id="s1", options={"tools": ""}, for_resume=False)
    i = argv.index("--tools")
    assert argv[i + 1] == ""


def test_build_argv_maps_many_fields():
    options = {
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
        "user_echo": True,
    }
    argv = build_claude_argv("claude", session_id="s1", options=options, for_resume=False)
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
    argv = build_claude_argv("claude", session_id="s1", options={}, for_resume=False)
    assert "--model" not in argv
    assert "--system-prompt" not in argv
    assert "--tools" not in argv


# ---------------------------------------------------------------------------
# user / interrupt / close (agent.user envelope)
# ---------------------------------------------------------------------------


def test_parse_user_message_string_content():
    u = parse_user(
        {
            "type": "agent.user",
            "session_id": "s1",
            "message": {"role": "user", "content": "hello"},
        }
    )
    assert u.message == {"role": "user", "content": "hello"}


def test_parse_user_message_list_content():
    blocks = [{"type": "text", "text": "hi"}]
    u = parse_user(
        {
            "type": "agent.user",
            "session_id": "s1",
            "message": {"role": "user", "content": blocks},
        }
    )
    assert u.message["content"] == blocks


def test_parse_user_rejects_missing_message():
    with pytest.raises(ProtocolError):
        parse_user({"type": "agent.user", "session_id": "s1"})


def test_parse_user_rejects_legacy_text_shorthand():
    with pytest.raises(ProtocolError):
        parse_user({"type": "agent.user", "session_id": "s1", "text": "hello"})


def test_parse_user_rejects_non_user_role():
    with pytest.raises(ProtocolError):
        parse_user(
            {
                "type": "agent.user",
                "session_id": "s1",
                "message": {"role": "assistant", "content": "x"},
            }
        )


def test_parse_user_rejects_non_string_non_list_content():
    with pytest.raises(ProtocolError):
        parse_user(
            {
                "type": "agent.user",
                "session_id": "s1",
                "message": {"role": "user", "content": 42},
            }
        )


def test_build_user_stdin_line_envelope_only():
    message = {"role": "user", "content": "hi"}
    line = build_user_stdin_line("s1", message=message)
    obj = json.loads(line)
    assert obj == {"type": "user", "message": message, "session_id": "s1"}
    assert obj["message"] == message
    assert line.endswith(b"\n")


def test_build_user_stdin_line_preserves_multimodal_blocks():
    blocks = [
        {"type": "text", "text": "What's this?"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "..."}},
    ]
    line = build_user_stdin_line("s1", message={"role": "user", "content": blocks})
    obj = json.loads(line)
    assert obj["message"]["content"] == blocks


def test_parse_interrupt_requires_session():
    with pytest.raises(ProtocolError):
        parse_interrupt({"type": "blemeesd.interrupt"})


def test_parse_close_defaults_delete_false():
    c = parse_close({"type": "blemeesd.close", "session_id": "s1"})
    assert c.delete is False


def test_parse_close_delete_true():
    c = parse_close({"type": "blemeesd.close", "session_id": "s1", "delete": True})
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
    msg = parse_list_sessions({"type": "blemeesd.list_sessions", "id": "r1", "cwd": "/home/u/proj"})
    assert msg.cwd == "/home/u/proj"
    assert msg.id == "r1"


# ---------------------------------------------------------------------------
# error_frame
# ---------------------------------------------------------------------------


def test_error_frame_includes_optional_fields():
    frame = error_frame("invalid_message", "oops", id="req_1", session_id="s1", backend="claude")
    assert frame["code"] == "invalid_message"
    assert frame["id"] == "req_1"
    assert frame["session_id"] == "s1"
    assert frame["backend"] == "claude"


def test_error_frame_omits_unset_ids():
    frame = error_frame("internal", "bad")
    assert "id" not in frame
    assert "session_id" not in frame
    assert "backend" not in frame


# ---------------------------------------------------------------------------
# ping / status
# ---------------------------------------------------------------------------


def test_parse_ping_ok_no_data():
    msg = parse_ping({"type": "blemeesd.ping"})
    assert msg.id is None


def test_parse_ping_ok_with_id_and_data():
    msg = parse_ping({"type": "blemeesd.ping", "id": "p1", "data": {"x": 1}})
    assert msg.id == "p1"
    assert msg.data == {"x": 1}


def test_parse_ping_rejects_extra_keys():
    with pytest.raises(ProtocolError, match="unexpected field"):
        parse_ping({"type": "blemeesd.ping", "bogus": True})


def test_parse_status_ok():
    msg = parse_status({"type": "blemeesd.status", "id": "s1"})
    assert msg.id == "s1"


def test_parse_status_rejects_extra_keys():
    with pytest.raises(ProtocolError, match="unexpected field"):
        parse_status({"type": "blemeesd.status", "extra": 1})


# ---------------------------------------------------------------------------
# Strict key checking (additionalProperties: false parity)
# ---------------------------------------------------------------------------


def test_parse_hello_rejects_extra_keys():
    with pytest.raises(ProtocolError, match="unexpected field"):
        parse_hello({"type": "blemeesd.hello", "protocol": "blemees/2", "unknown": True})


def test_parse_user_rejects_extra_keys():
    with pytest.raises(ProtocolError, match="unexpected field"):
        parse_user(
            {
                "type": "agent.user",
                "session_id": "s1",
                "message": {"role": "user", "content": "hi"},
                "extra": "oops",
            }
        )


def test_parse_interrupt_rejects_extra_keys():
    with pytest.raises(ProtocolError, match="unexpected field"):
        parse_interrupt({"type": "blemeesd.interrupt", "session_id": "s1", "extra": 1})


def test_parse_close_rejects_extra_keys():
    with pytest.raises(ProtocolError, match="unexpected field"):
        parse_close({"type": "blemeesd.close", "session_id": "s1", "extra": 1})


def test_parse_list_sessions_rejects_extra_keys():
    with pytest.raises(ProtocolError, match="unexpected field"):
        parse_list_sessions({"type": "blemeesd.list_sessions", "cwd": "/tmp", "extra": 1})


def test_parse_watch_rejects_extra_keys():
    with pytest.raises(ProtocolError, match="unexpected field"):
        parse_watch({"type": "blemeesd.watch", "session_id": "s1", "extra": 1})


def test_parse_unwatch_rejects_extra_keys():
    with pytest.raises(ProtocolError, match="unexpected field"):
        parse_unwatch({"type": "blemeesd.unwatch", "session_id": "s1", "extra": 1})


def test_parse_session_info_rejects_extra_keys():
    with pytest.raises(ProtocolError, match="unexpected field"):
        parse_session_info({"type": "blemeesd.session_info", "session_id": "s1", "extra": 1})


def test_parse_open_rejects_extra_keys():
    with pytest.raises(ProtocolError, match="unexpected field"):
        parse_open(_open_frame(unknown_flag="value"))
