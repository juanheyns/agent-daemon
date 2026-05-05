"""Row-by-row tests for the Codex MCP → agent.* translator.

The fixtures are extracted verbatim from
``docs/traces/codex-mcp-turn-20260427T144241.jsonl`` so every test
exercises a real wire shape rather than a fabrication.
"""

from __future__ import annotations

from blemees_agent.backends.translate_codex import CodexTranslator

THREAD_ID = "019dd03f-e946-7dd3-a0e4-3a3db8146dae"
META = {"requestId": 3, "threadId": THREAD_ID}


# ---------------------------------------------------------------------------
# session_configured → agent.system_init
# ---------------------------------------------------------------------------


def test_session_configured_emits_system_init():
    msg = {
        "type": "session_configured",
        "session_id": THREAD_ID,
        "model": "gpt-5.4",
        "model_provider_id": "openai",
        "approval_policy": "never",
        "sandbox_policy": {"type": "read-only"},
        "permission_profile": {"type": "managed"},
        "cwd": "/tmp/wd",
        "reasoning_effort": "high",
        "rollout_path": "/Users/u/.codex/sessions/x.jsonl",
        "history_log_id": 1,
    }
    out = CodexTranslator().translate_event(msg, meta=META)
    assert len(out) == 1
    init = out[0]
    assert init["type"] == "agent.system_init"
    assert init["model"] == "gpt-5.4"
    assert init["cwd"] == "/tmp/wd"
    assert init["native_session_id"] == THREAD_ID
    assert init["capabilities"] == {
        "sandbox_policy": {"type": "read-only"},
        "approval_policy": "never",
        "permission_profile": {"type": "managed"},
        "reasoning_effort": "high",
        "rollout_path": "/Users/u/.codex/sessions/x.jsonl",
    }
    assert "raw" not in init
    assert "history_log_id" not in init  # routed under raw only


def test_session_configured_carries_raw_when_opted_in():
    msg = {
        "type": "session_configured",
        "session_id": THREAD_ID,
        "model": "gpt-5.4",
    }
    t = CodexTranslator(include_raw=True)
    [init] = t.translate_event(msg, meta=META)
    assert init["raw"]["type"] == "session_configured"
    assert init["raw"]["_meta"] == META


# ---------------------------------------------------------------------------
# task_started → agent.notice (or context-window fold-in)
# ---------------------------------------------------------------------------


def test_task_started_emits_notice_when_init_already_emitted():
    """Codex's `started_at` (Unix seconds) is normalised to
    `started_at_ms` (Unix milliseconds) so the field name + unit
    match claude's daemon-synth `task_started.data.started_at_ms`."""
    t = CodexTranslator()
    t.translate_event(
        {"type": "session_configured", "session_id": THREAD_ID, "model": "m"}, meta=META
    )
    out = t.translate_event(
        {
            "type": "task_started",
            "turn_id": "3",
            "started_at": 1777315342,
            "model_context_window": 258400,
        },
        meta=META,
    )
    assert len(out) == 1
    notice = out[0]
    assert notice["type"] == "agent.notice"
    assert notice["category"] == "task_started"
    assert notice["data"] == {
        "turn_id": "3",
        "started_at_ms": 1777315342_000,
        "model_context_window": 258400,
    }


def test_task_started_before_init_folds_context_window():
    t = CodexTranslator()
    t.translate_event(
        {"type": "task_started", "turn_id": "1", "model_context_window": 200},
        meta=META,
    )
    [init] = t.translate_event(
        {"type": "session_configured", "session_id": THREAD_ID, "model": "m"},
        meta=META,
    )
    assert init["context_window"] == 200


# ---------------------------------------------------------------------------
# mcp_startup_* → notices
# ---------------------------------------------------------------------------


def test_mcp_startup_update_is_notice():
    out = CodexTranslator().translate_event(
        {
            "type": "mcp_startup_update",
            "server": "codex_apps",
            "status": {"state": "ready"},
        },
        meta=META,
    )
    assert out == [
        {
            "type": "agent.notice",
            "level": "info",
            "category": "backend_mcp_startup",
            "data": {"server": "codex_apps", "status": {"state": "ready"}},
        }
    ]


def test_mcp_startup_complete_is_notice():
    out = CodexTranslator().translate_event(
        {"type": "mcp_startup_complete", "ready": ["codex_apps"], "failed": [], "cancelled": []},
        meta=META,
    )
    assert out[0]["category"] == "backend_mcp_startup_complete"
    assert out[0]["data"] == {"ready": ["codex_apps"], "failed": [], "cancelled": []}


# ---------------------------------------------------------------------------
# agent_message_content_delta → agent.delta
# ---------------------------------------------------------------------------


def test_content_delta_emits_text_delta_with_item_id():
    out = CodexTranslator().translate_event(
        {
            "type": "agent_message_content_delta",
            "item_id": "msg_123",
            "delta": "pong",
        },
        meta=META,
    )
    assert out == [
        {
            "type": "agent.delta",
            "kind": "text",
            "text": "pong",
            "item_id": "msg_123",
        }
    ]


def test_agent_message_delta_is_dropped_as_duplicate():
    out = CodexTranslator().translate_event(
        {"type": "agent_message_delta", "delta": "pong"}, meta=META
    )
    assert out == []


def test_agent_message_is_dropped_as_duplicate():
    out = CodexTranslator().translate_event(
        {"type": "agent_message", "message": "pong.", "phase": "final_answer"}, meta=META
    )
    assert out == []


def test_user_message_is_dropped_as_duplicate():
    out = CodexTranslator().translate_event({"type": "user_message", "message": "hi"}, meta=META)
    assert out == []


# ---------------------------------------------------------------------------
# item_completed → agent.message / agent.user_echo
# ---------------------------------------------------------------------------


def test_item_completed_agent_message():
    msg = {
        "type": "item_completed",
        "thread_id": THREAD_ID,
        "turn_id": "3",
        "item": {
            "type": "AgentMessage",
            "id": "msg_1",
            "content": [{"type": "Text", "text": "pong."}],
            "phase": "final_answer",
        },
    }
    [frame] = CodexTranslator().translate_event(msg, meta=META)
    assert frame["type"] == "agent.message"
    assert frame["role"] == "assistant"
    assert frame["content"] == [{"type": "text", "text": "pong."}]
    assert frame["phase"] == "final_answer"


def test_item_completed_user_message_dropped_when_user_echo_off():
    """Default: user_echo=False, so item_completed{UserMessage} is
    suppressed. Symmetric with claude (which only emits user_echo
    when --replay-user-messages is set).
    """
    msg = {
        "type": "item_completed",
        "item": {
            "type": "UserMessage",
            "id": "u1",
            "content": [{"type": "text", "text": "hi"}],
        },
    }
    assert CodexTranslator().translate_event(msg, meta=META) == []


def test_item_completed_user_message_emitted_when_user_echo_on():
    msg = {
        "type": "item_completed",
        "item": {
            "type": "UserMessage",
            "id": "u1",
            "content": [{"type": "text", "text": "hi"}],
        },
    }
    [frame] = CodexTranslator(user_echo=True).translate_event(msg, meta=META)
    assert frame["type"] == "agent.user_echo"
    assert frame["message"]["role"] == "user"
    assert frame["message"]["content"] == [{"type": "text", "text": "hi"}]


def test_item_completed_reasoning_is_dropped():
    msg = {
        "type": "item_completed",
        "item": {"type": "Reasoning", "id": "rs_1", "summary_text": []},
    }
    assert CodexTranslator().translate_event(msg, meta=META) == []


def test_item_started_is_always_dropped():
    msg = {
        "type": "item_started",
        "item": {"type": "AgentMessage", "id": "msg_1", "content": []},
    }
    assert CodexTranslator().translate_event(msg, meta=META) == []


# ---------------------------------------------------------------------------
# raw_response_item → dropped from primary stream
# ---------------------------------------------------------------------------


def test_raw_response_item_is_dropped():
    msg = {
        "type": "raw_response_item",
        "item": {"type": "message", "role": "assistant", "content": []},
    }
    assert CodexTranslator().translate_event(msg, meta=META) == []


# ---------------------------------------------------------------------------
# token_count
# ---------------------------------------------------------------------------


def test_token_count_mid_turn_is_rate_limit_notice():
    """Codex's mid-turn rate-limit ping is normalised into the unified
    `data.limit` shape — vendor extras (plan_type, limit_id, …) go
    under `data.vendor`. See agent-events.md `rate_limits` row."""
    msg = {
        "type": "token_count",
        "info": None,
        "rate_limits": {
            "primary": {
                "resets_at": 1777599825,
                "used_percent": 16.0,
                "window_minutes": 10080,
            },
            "secondary": None,
            "plan_type": "free",
            "limit_id": "codex",
        },
    }
    [notice] = CodexTranslator().translate_event(msg, meta=META)
    assert notice["category"] == "rate_limits"
    assert notice["data"] == {
        "limit": {
            "resets_at_ms": 1777599825_000,
            "used_percent": 16.0,
            "window_minutes": 10080,
        },
        "vendor": {
            "plan_type": "free",
            "limit_id": "codex",
        },
    }


def test_token_count_mid_turn_includes_secondary_limit_when_present():
    msg = {
        "type": "token_count",
        "info": None,
        "rate_limits": {
            "primary": {"resets_at": 1777315342, "used_percent": 12.5},
            "secondary": {"resets_at": 1777315500, "used_percent": 88.0, "window_minutes": 300},
        },
    }
    [notice] = CodexTranslator().translate_event(msg, meta=META)
    assert notice["data"]["secondary_limit"] == {
        "resets_at_ms": 1777315500_000,
        "used_percent": 88.0,
        "window_minutes": 300,
    }


def test_token_count_mid_turn_with_no_rate_limits_emits_empty_notice():
    """`rate_limits` missing or non-dict → category-only notice (no data)."""
    msg = {"type": "token_count", "info": None}
    [notice] = CodexTranslator().translate_event(msg, meta=META)
    assert notice["category"] == "rate_limits"
    assert "data" not in notice


def test_token_count_final_is_buffered_for_result():
    t = CodexTranslator()
    info = {
        "total_token_usage": {"input_tokens": 100, "output_tokens": 5},
        "last_token_usage": {
            "input_tokens": 100,
            "cached_input_tokens": 20,
            "output_tokens": 5,
            "reasoning_output_tokens": 7,
        },
        "model_context_window": 200,
    }
    out = t.translate_event({"type": "token_count", "info": info, "rate_limits": {}}, meta=META)
    assert out == []  # buffered, not emitted directly
    result = t.finalize_success()
    assert result["usage"] == {
        "input_tokens": 100,
        "output_tokens": 5,
        "cache_read_input_tokens": 20,
        "reasoning_output_tokens": 7,
    }


# ---------------------------------------------------------------------------
# task_complete + finalize_success → synthesised agent.result
# ---------------------------------------------------------------------------


def test_task_complete_buffered_then_result_synthesised():
    t = CodexTranslator()
    out = t.translate_event(
        {
            "type": "task_complete",
            "turn_id": "3",
            "duration_ms": 4371,
            "time_to_first_token_ms": 4201,
            "last_agent_message": "pong.",
        },
        meta=META,
    )
    assert out == []
    result = t.finalize_success()
    assert result == {
        "type": "agent.result",
        "subtype": "success",
        "num_turns": 1,
        "duration_ms": 4371,
        "turn_id": "3",
        "time_to_first_token_ms": 4201,
    }


def test_finalize_resets_buffered_state():
    t = CodexTranslator()
    t.translate_event({"type": "task_complete", "duration_ms": 1}, meta=META)
    t.finalize_success()
    second = t.finalize_success()
    assert "duration_ms" not in second  # cleared after first finalize


def test_finalize_error_carries_error_payload():
    t = CodexTranslator()
    err = {"code": -32000, "message": "boom"}
    result = t.finalize_error(err)
    assert result["subtype"] == "error"
    assert result["error"] == err


def test_finalize_interrupted_marks_subtype():
    t = CodexTranslator()
    result = t.finalize_interrupted()
    assert result["type"] == "agent.result"
    assert result["subtype"] == "interrupted"


def test_turn_aborted_buffers_for_finalize_interrupted():
    """Codex 0.125.x emits `turn_aborted` after `notifications/cancelled`.

    The translator buffers turn_id without emitting a frame; the
    backend's cancellation path then calls finalize_interrupted, which
    should surface the buffered turn_id on the synthesised
    agent.result.
    """
    t = CodexTranslator()
    out = t.translate_event(
        {"type": "turn_aborted", "turn_id": "7", "reason": "user_interrupt"},
        meta=META,
    )
    assert out == []  # no frame from turn_aborted itself
    result = t.finalize_interrupted()
    assert result == {
        "type": "agent.result",
        "subtype": "interrupted",
        "num_turns": 1,
        "turn_id": "7",
    }


def test_turn_aborted_does_not_clobber_existing_task_complete():
    t = CodexTranslator()
    t.translate_event(
        {"type": "task_complete", "turn_id": "5", "duration_ms": 4321},
        meta=META,
    )
    t.translate_event(
        {"type": "turn_aborted", "reason": "user_interrupt"},
        meta=META,
    )
    result = t.finalize_interrupted()
    # Existing fields preserved; turn_aborted just merges its own.
    assert result["duration_ms"] == 4321
    assert result["turn_id"] == "5"


# ---------------------------------------------------------------------------
# Unknown event types → fallback notice
# ---------------------------------------------------------------------------


def test_unknown_msg_type_falls_through_to_notice():
    out = CodexTranslator().translate_event({"type": "future_event", "data": 1}, meta=META)
    assert out == [
        {
            "type": "agent.notice",
            "level": "info",
            "category": "codex_unknown_future_event",
        }
    ]


def test_non_dict_msg_returns_empty():
    assert CodexTranslator().translate_event(None, meta=META) == []  # type: ignore[arg-type]
    assert CodexTranslator().translate_event({"no_type": 1}, meta=META) == []


# ---------------------------------------------------------------------------
# exec_command_* (mapping locked from spec; will be re-verified once we
# capture a real tool-using trace)
# ---------------------------------------------------------------------------


def test_exec_command_begin_to_tool_use():
    msg = {
        "type": "exec_command_begin",
        "call_id": "call_1",
        "command": ["ls", "-la"],
    }
    [frame] = CodexTranslator().translate_event(msg, meta=META)
    assert frame["type"] == "agent.tool_use"
    assert frame["tool_use_id"] == "call_1"
    assert frame["name"] == "shell"
    assert frame["input"] == ["ls", "-la"]


def test_exec_command_end_to_tool_result():
    msg = {
        "type": "exec_command_end",
        "call_id": "call_1",
        "output": "drwx",
        "is_error": False,
    }
    [frame] = CodexTranslator().translate_event(msg, meta=META)
    assert frame["type"] == "agent.tool_result"
    assert frame["tool_use_id"] == "call_1"
    assert frame["output"] == "drwx"
    assert frame["is_error"] is False
