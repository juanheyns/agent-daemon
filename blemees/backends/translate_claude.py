"""Translate Claude Code stream-json events into `agent.*` frames.

Pure functions — no I/O, no global state. The mapping table is
documented in `docs/agent-events.md`. Tests in
`tests/blemees/test_translate_claude.py` pin the row-by-row contract.

Each translator function takes one CC native event dict (un-prefixed,
straight off the `claude -p` stdout reader) and returns a list of
zero or more `agent.*` frames. Returning multiple frames is rare but
necessary for CC `user`-type events carrying `tool_result` blocks,
which fan out into `agent.tool_result` plus optionally `agent.user_echo`.

Common envelope fields (`session_id`, `seq`, `backend`) are NOT set
here — the caller (the backend's stdout reader) layers them on. This
keeps the translator stateless and trivially unit-testable.
"""

from __future__ import annotations

from typing import Any

# A "marker" returned alongside frames to flag turn-end so the caller
# can flip `turn_active=False` without rummaging through frame types.
TURN_END_TYPES: frozenset[str] = frozenset({"agent.result"})


def translate_event(event: dict[str, Any], *, include_raw: bool = False) -> list[dict[str, Any]]:
    """Translate one CC native event into agent.* frames.

    `event` is the raw stream-json dict CC writes to stdout. The
    caller is responsible for adding `session_id`, `seq`, and
    `backend` to each returned frame.
    """
    cc_type = event.get("type")
    if not isinstance(cc_type, str):
        return []

    raw = dict(event) if include_raw else None

    if cc_type == "system":
        return _translate_system(event, raw)
    if cc_type == "stream_event":
        return _translate_stream_event(event, raw)
    if cc_type == "assistant":
        return _translate_assistant(event, raw)
    if cc_type == "user":
        return _translate_user(event, raw)
    if cc_type == "result":
        return [_translate_result(event, raw)]
    if cc_type == "rate_limit_event":
        return _translate_rate_limit_event(event, raw)
    if cc_type == "partial_assistant":
        # Drop — redundant once we emit the deltas individually.
        return []

    # Unknown native type: surface as a notice so we never silently lose
    # data, and so future CC events propagate without a code change.
    notice: dict[str, Any] = {
        "type": "agent.notice",
        "level": "info",
        "category": f"claude_unknown_{cc_type}",
    }
    if raw is not None:
        notice["raw"] = raw
    return [notice]


# ---------------------------------------------------------------------------
# `system`
# ---------------------------------------------------------------------------


def _translate_system(event: dict[str, Any], raw: dict[str, Any] | None) -> list[dict[str, Any]]:
    subtype = event.get("subtype")
    if subtype == "init":
        out: dict[str, Any] = {"type": "agent.system_init"}
        if isinstance(event.get("model"), str):
            out["model"] = event["model"]
        if isinstance(event.get("cwd"), str):
            out["cwd"] = event["cwd"]
        if isinstance(event.get("tools"), list):
            out["tools"] = [t for t in event["tools"] if isinstance(t, str)]
        # CC's session id arrives via the daemon-known `--session-id` flag,
        # not an in-band field, so we don't set `native_session_id` here —
        # the daemon adds it from the `Session.session_id` it spawned with.
        if raw is not None:
            out["raw"] = raw
        return [out]

    # Other system subtypes — surface as notices.
    notice: dict[str, Any] = {
        "type": "agent.notice",
        "level": "info",
        "category": f"claude_system_{subtype}" if isinstance(subtype, str) else "claude_system",
    }
    data = {k: v for k, v in event.items() if k not in ("type", "subtype")}
    if data:
        notice["data"] = data
    if raw is not None:
        notice["raw"] = raw
    return [notice]


# ---------------------------------------------------------------------------
# `stream_event` (Anthropic Messages API MessageStreamEvent)
# ---------------------------------------------------------------------------


def _translate_stream_event(
    event: dict[str, Any], raw: dict[str, Any] | None
) -> list[dict[str, Any]]:
    inner = event.get("event")
    if not isinstance(inner, dict):
        return []
    inner_type = inner.get("type")
    index = inner.get("index")

    if inner_type == "content_block_start":
        block = inner.get("content_block") or {}
        block_type = block.get("type")
        if block_type == "tool_use":
            tool_use_id = block.get("id")
            name = block.get("name")
            if isinstance(tool_use_id, str) and isinstance(name, str):
                frame: dict[str, Any] = {
                    "type": "agent.tool_use",
                    "tool_use_id": tool_use_id,
                    "name": name,
                    "input": block.get("input") or {},
                }
                if isinstance(index, int):
                    frame["index"] = index
                if raw is not None:
                    frame["raw"] = raw
                return [frame]
        # text / thinking content_block_start: drop (deltas carry the data).
        return []

    if inner_type == "content_block_delta":
        delta = inner.get("delta") or {}
        delta_type = delta.get("type")
        if delta_type == "text_delta":
            text = delta.get("text", "")
            frame = {"type": "agent.delta", "kind": "text", "text": text}
        elif delta_type == "thinking_delta":
            text = delta.get("thinking", "")
            frame = {"type": "agent.delta", "kind": "thinking", "text": text}
        elif delta_type == "input_json_delta":
            partial_json = delta.get("partial_json", "")
            frame = {
                "type": "agent.delta",
                "kind": "tool_input",
                "partial_json": partial_json,
            }
        else:
            # Unknown delta type — preserve as notice so we don't silently lose data.
            return [
                {
                    "type": "agent.notice",
                    "level": "info",
                    "category": f"claude_delta_{delta_type}"
                    if isinstance(delta_type, str)
                    else "claude_delta",
                    "data": {"delta": delta, "index": index},
                    **({"raw": raw} if raw is not None else {}),
                }
            ]
        if isinstance(index, int):
            frame["index"] = index
        if raw is not None:
            frame["raw"] = raw
        return [frame]

    # message_start / message_delta / message_stop / content_block_stop:
    # carry no client-relevant data the daemon can't reconstruct, drop.
    return []


# ---------------------------------------------------------------------------
# `assistant`
# ---------------------------------------------------------------------------


def _translate_assistant(event: dict[str, Any], raw: dict[str, Any] | None) -> list[dict[str, Any]]:
    message = event.get("message") or {}
    content = message.get("content")
    if not isinstance(content, list):
        # CC always emits a list, but be permissive — if a string ever shows
        # up, wrap it.
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        else:
            content = []
    frame: dict[str, Any] = {
        "type": "agent.message",
        "role": "assistant",
        "content": content,
    }
    if raw is not None:
        frame["raw"] = raw
    return [frame]


# ---------------------------------------------------------------------------
# `user`
# ---------------------------------------------------------------------------


def _translate_user(event: dict[str, Any], raw: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Split CC's user-echo into `agent.tool_result` frames + `agent.user_echo`.

    CC re-emits the user message after each turn for transcript fidelity,
    *and* injects synthetic user turns whose content is a list of
    `tool_result` blocks (one per tool call the previous turn made). We
    promote each `tool_result` block to its own `agent.tool_result` and
    keep any remaining text blocks as `agent.user_echo`.
    """
    message = event.get("message") or {}
    content = message.get("content")
    out: list[dict[str, Any]] = []

    if isinstance(content, list):
        leftover_blocks: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                tool_use_id = block.get("tool_use_id")
                if isinstance(tool_use_id, str):
                    tr: dict[str, Any] = {
                        "type": "agent.tool_result",
                        "tool_use_id": tool_use_id,
                        "output": block.get("content"),
                    }
                    if "is_error" in block:
                        tr["is_error"] = bool(block["is_error"])
                    if raw is not None:
                        tr["raw"] = raw
                    out.append(tr)
                continue
            leftover_blocks.append(block)
        if leftover_blocks:
            echo_msg = {"role": "user", "content": leftover_blocks}
            echo: dict[str, Any] = {"type": "agent.user_echo", "message": echo_msg}
            if raw is not None:
                echo["raw"] = raw
            out.append(echo)
        elif not out:
            # All blocks were filtered (rare); still emit an echo so the
            # client sees the turn boundary.
            echo_msg = {"role": "user", "content": []}
            echo = {"type": "agent.user_echo", "message": echo_msg}
            if raw is not None:
                echo["raw"] = raw
            out.append(echo)
        return out

    # String content (or anything else): plain user echo.
    echo_msg = {
        "role": "user",
        "content": content if isinstance(content, str) else "",
    }
    echo = {"type": "agent.user_echo", "message": echo_msg}
    if raw is not None:
        echo["raw"] = raw
    return [echo]


# ---------------------------------------------------------------------------
# `rate_limit_event`
# ---------------------------------------------------------------------------


def _translate_rate_limit_event(
    event: dict[str, Any], raw: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """CC's per-turn rate-limit ping → unified `rate_limits` notice.

    Mirrors codex's `token_count{info:null, rate_limits}` mapping
    (translate_codex._translate_token_count) so a client that filters
    `agent.notice` by category sees the same `"rate_limits"` value
    on both backends. We don't pin CC's payload shape — pass every
    field except `type` through under `data` so future CC additions
    propagate without a code change.
    """
    data = {k: v for k, v in event.items() if k != "type"}
    notice: dict[str, Any] = {
        "type": "agent.notice",
        "level": "info",
        "category": "rate_limits",
    }
    if data:
        notice["data"] = data
    if raw is not None:
        notice["raw"] = raw
    return [notice]


# ---------------------------------------------------------------------------
# `result`
# ---------------------------------------------------------------------------


def _translate_result(event: dict[str, Any], raw: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "type": "agent.result",
        "subtype": event.get("subtype", "success"),
    }
    for key in ("duration_ms", "num_turns"):
        if isinstance(event.get(key), int):
            out[key] = event[key]
    usage = event.get("usage")
    if isinstance(usage, dict):
        # Pass through verbatim — CC's keys (`input_tokens`,
        # `cache_read_input_tokens`, …) already match the
        # `NormalisedUsage` $def. Future Anthropic-added integer fields
        # appear automatically (additionalProperties on the schema).
        out["usage"] = {k: v for k, v in usage.items() if isinstance(v, int)}
    if raw is not None:
        out["raw"] = raw
    return out
