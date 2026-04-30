"""Translate Codex MCP events into `agent.*` frames.

The mapping is documented in `docs/agent-events.md`. Tests in
`tests/blemees/test_translate_codex.py` pin the row-by-row contract.

Unlike the Claude translator (purely stateless per-event), Codex needs a
small per-turn buffer: the synthesised ``agent.result`` is assembled at
turn end from the *last* ``token_count`` (with ``info`` populated) plus
the ``task_complete`` event, plus the JSON-RPC ``result`` envelope of
the originating ``tools/call``. We keep that state on a
:class:`CodexTranslator` instance — one instance per backend, since the
fields it caches reset cleanly each turn (``finalize_*`` clears them).

Common envelope fields (``session_id``, ``seq``, ``backend``) are NOT
set here — the caller (the backend's stdout reader) layers them on, the
same as the Claude path.
"""

from __future__ import annotations

from typing import Any


class CodexTranslator:
    """Per-backend stateful translator for Codex MCP events.

    The translator is instantiated by the backend at spawn time and
    drives the entire session — `session_configured` arrives once,
    every subsequent turn produces `task_started` … `task_complete`
    plus a final `token_count` we hold for the synthesised
    `agent.result`.
    """

    def __init__(self, *, include_raw: bool = False, user_echo: bool = False) -> None:
        self._include_raw = include_raw
        # When False (the default), drop `item_completed{UserMessage}`
        # from the primary stream so codex matches the daemon's
        # default-off `user_echo` policy on both backends. Clients that
        # want the echoes opt in via `options.codex.user_echo: true`.
        self._user_echo = user_echo
        # Whether `agent.system_init` has been emitted for this backend.
        self._system_init_emitted: bool = False
        # Native session id surfaced on system_init (and used by the
        # backend driver to populate `blemeesd.opened.native_session_id`).
        self.thread_id: str | None = None
        # Buffered for `agent.result` synthesis at turn end.
        self._last_token_usage: dict[str, Any] | None = None
        self._task_complete: dict[str, Any] | None = None
        # In case `task_started` arrives before `session_configured` —
        # we still want `context_window` on the eventual init frame.
        self._pending_context_window: int | None = None

    # ------------------------------------------------------------------
    # Per-event dispatch
    # ------------------------------------------------------------------

    def translate_event(
        self,
        msg: dict[str, Any],
        *,
        meta: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Translate one ``codex/event`` body into agent.* frames.

        ``msg`` is the inner ``params.msg`` dict; ``meta`` is the sibling
        ``params._meta`` (carrying ``threadId``, ``requestId``). Returns
        a list of frames (often empty — many native event types are
        intentionally dropped from the primary stream).
        """
        if not isinstance(msg, dict):
            return []
        msg_type = msg.get("type")
        if not isinstance(msg_type, str):
            return []

        raw = self._raw_for(msg, meta)

        if msg_type == "session_configured":
            return self._translate_session_configured(msg, raw)
        if msg_type == "task_started":
            return self._translate_task_started(msg, raw)
        if msg_type == "task_complete":
            return self._translate_task_complete(msg, raw)
        if msg_type == "turn_aborted":
            return self._translate_turn_aborted(msg, raw)
        if msg_type == "token_count":
            return self._translate_token_count(msg, raw)
        if msg_type in ("mcp_startup_update", "mcp_startup_complete"):
            return self._translate_mcp_startup(msg_type, msg, raw)
        if msg_type == "agent_message_content_delta":
            return self._translate_content_delta(msg, raw)
        if msg_type == "agent_message_delta":
            # Duplicate of agent_message_content_delta (without item_id).
            return []
        if msg_type == "agent_message":
            # Duplicate of item_completed{AgentMessage}.
            return []
        if msg_type == "user_message":
            # Duplicate of item_completed{UserMessage}.
            return []
        if msg_type == "item_started":
            return self._translate_item_started(msg, raw)
        if msg_type == "item_completed":
            return self._translate_item_completed(msg, raw)
        if msg_type == "raw_response_item":
            # Surfaced under `raw` only when include_raw_events is set; we
            # have no agent.* equivalent.
            return []
        if msg_type.startswith("exec_command_"):
            return self._translate_exec_command(msg_type, msg, raw)

        # Unknown native type — surface as a notice so we never silently
        # lose data.
        notice: dict[str, Any] = {
            "type": "agent.notice",
            "level": "info",
            "category": f"codex_unknown_{msg_type}",
        }
        if raw is not None:
            notice["raw"] = raw
        return [notice]

    # ------------------------------------------------------------------
    # `session_configured`
    # ------------------------------------------------------------------

    def _translate_session_configured(
        self, msg: dict[str, Any], raw: dict[str, Any] | None
    ) -> list[dict[str, Any]]:
        frame: dict[str, Any] = {"type": "agent.system_init"}
        if isinstance(msg.get("model"), str):
            frame["model"] = msg["model"]
        if isinstance(msg.get("cwd"), str):
            frame["cwd"] = msg["cwd"]
        sid = msg.get("session_id")
        if isinstance(sid, str):
            frame["native_session_id"] = sid
            self.thread_id = sid

        capabilities: dict[str, Any] = {}
        for key in (
            "sandbox_policy",
            "approval_policy",
            "permission_profile",
            "reasoning_effort",
            "rollout_path",
        ):
            if key in msg and msg[key] is not None:
                capabilities[key] = msg[key]
        if capabilities:
            frame["capabilities"] = capabilities

        if self._pending_context_window is not None:
            frame["context_window"] = self._pending_context_window
            self._pending_context_window = None

        if raw is not None:
            frame["raw"] = raw
        self._system_init_emitted = True
        return [frame]

    # ------------------------------------------------------------------
    # `task_started`
    # ------------------------------------------------------------------

    def _translate_task_started(
        self, msg: dict[str, Any], raw: dict[str, Any] | None
    ) -> list[dict[str, Any]]:
        ctx = msg.get("model_context_window")

        # Init not yet emitted (unusual ordering): fold context window
        # into the eventual system_init and skip the notice.
        if not self._system_init_emitted and isinstance(ctx, int):
            self._pending_context_window = ctx

        data: dict[str, Any] = {}
        for key in ("turn_id", "model_context_window"):
            if key in msg and msg[key] is not None:
                data[key] = msg[key]
        # Codex sends `started_at` as Unix **seconds** (e.g.
        # `1777315342`). Normalise to `started_at_ms` (Unix
        # milliseconds) so the field aligns with the daemon's
        # ms-everywhere convention and with claude's synth
        # `task_started.data.started_at_ms`.
        started_at = msg.get("started_at")
        if isinstance(started_at, (int, float)):
            data["started_at_ms"] = int(started_at * 1000)

        notice: dict[str, Any] = {
            "type": "agent.notice",
            "level": "info",
            "category": "task_started",
        }
        if data:
            notice["data"] = data
        if raw is not None:
            notice["raw"] = raw
        return [notice]

    # ------------------------------------------------------------------
    # `task_complete`
    # ------------------------------------------------------------------

    def _translate_task_complete(
        self, msg: dict[str, Any], _raw: dict[str, Any] | None
    ) -> list[dict[str, Any]]:
        # Buffered — the synthesised agent.result picks these up at
        # turn end via ``finalize_success`` / ``finalize_error``.
        buf: dict[str, Any] = {}
        for key in ("turn_id", "duration_ms", "time_to_first_token_ms"):
            if key in msg and msg[key] is not None:
                buf[key] = msg[key]
        self._task_complete = buf
        return []

    # ------------------------------------------------------------------
    # `turn_aborted`
    # ------------------------------------------------------------------

    def _translate_turn_aborted(
        self, msg: dict[str, Any], _raw: dict[str, Any] | None
    ) -> list[dict[str, Any]]:
        # Codex 0.125.x emits `turn_aborted` when a `notifications/cancelled`
        # request lands during a tools/call. We buffer the turn-id (if any)
        # so the synthesised `agent.result{subtype:"interrupted"}` can
        # carry it; the JSON-RPC response that *follows* (if any) is
        # treated as a no-op by the backend, since we'll have already
        # finalised the turn from this event.
        buf: dict[str, Any] = {}
        for key in ("turn_id", "reason"):
            if key in msg and msg[key] is not None:
                buf[key] = msg[key]
        if buf:
            # Reuse the task_complete buffer slot — finalize_* reads
            # `duration_ms`/`turn_id`/`time_to_first_token_ms` from it
            # but tolerates missing keys.
            self._task_complete = {**(self._task_complete or {}), **buf}
        return []

    # ------------------------------------------------------------------
    # `token_count`
    # ------------------------------------------------------------------

    def _translate_token_count(
        self, msg: dict[str, Any], raw: dict[str, Any] | None
    ) -> list[dict[str, Any]]:
        info = msg.get("info")
        if info is None:
            # Mid-turn rate-limit ping. Surface as a notice with the
            # unified `data.limit` envelope (see agent-events.md);
            # vendor-specific extras go under `data.vendor`.
            data = _normalise_rate_limits_codex(msg.get("rate_limits"))
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

        if isinstance(info, dict):
            self._last_token_usage = info
        return []

    # ------------------------------------------------------------------
    # MCP startup notices
    # ------------------------------------------------------------------

    def _translate_mcp_startup(
        self, msg_type: str, msg: dict[str, Any], raw: dict[str, Any] | None
    ) -> list[dict[str, Any]]:
        if msg_type == "mcp_startup_update":
            data: dict[str, Any] = {}
            if "server" in msg:
                data["server"] = msg["server"]
            if "status" in msg:
                data["status"] = msg["status"]
            notice: dict[str, Any] = {
                "type": "agent.notice",
                "level": "info",
                "category": "backend_mcp_startup",
            }
            if data:
                notice["data"] = data
            if raw is not None:
                notice["raw"] = raw
            return [notice]

        # mcp_startup_complete
        data = {}
        for key in ("ready", "failed", "cancelled"):
            if key in msg and msg[key] is not None:
                data[key] = msg[key]
        notice = {
            "type": "agent.notice",
            "level": "info",
            "category": "backend_mcp_startup_complete",
        }
        if data:
            notice["data"] = data
        if raw is not None:
            notice["raw"] = raw
        return [notice]

    # ------------------------------------------------------------------
    # `agent_message_content_delta`
    # ------------------------------------------------------------------

    def _translate_content_delta(
        self, msg: dict[str, Any], raw: dict[str, Any] | None
    ) -> list[dict[str, Any]]:
        delta = msg.get("delta", "")
        if not isinstance(delta, str):
            return []
        frame: dict[str, Any] = {
            "type": "agent.delta",
            "kind": "text",
            "text": delta,
        }
        item_id = msg.get("item_id")
        if isinstance(item_id, str):
            frame["item_id"] = item_id
        if raw is not None:
            frame["raw"] = raw
        return [frame]

    # ------------------------------------------------------------------
    # `item_started` / `item_completed`
    # ------------------------------------------------------------------

    def _translate_item_started(
        self, msg: dict[str, Any], _raw: dict[str, Any] | None
    ) -> list[dict[str, Any]]:
        # We wait for `item_completed` for UserMessage / AgentMessage and
        # drop Reasoning entirely from the primary stream.
        return []

    def _translate_item_completed(
        self, msg: dict[str, Any], raw: dict[str, Any] | None
    ) -> list[dict[str, Any]]:
        item = msg.get("item") or {}
        if not isinstance(item, dict):
            return []
        item_type = item.get("type")
        if item_type == "UserMessage":
            return self._user_echo_from_item(item, raw)
        if item_type == "AgentMessage":
            return self._assistant_message_from_item(item, raw)
        # Reasoning, etc. — drop from primary stream.
        return []

    def _user_echo_from_item(
        self, item: dict[str, Any], raw: dict[str, Any] | None
    ) -> list[dict[str, Any]]:
        if not self._user_echo:
            # Default: drop. See `__init__` — codex matches claude's
            # default-off `user_echo` policy unless the client opts in.
            return []
        content = _normalise_codex_content(item.get("content"))
        echo: dict[str, Any] = {
            "type": "agent.user_echo",
            "message": {"role": "user", "content": content},
        }
        if raw is not None:
            echo["raw"] = raw
        return [echo]

    def _assistant_message_from_item(
        self, item: dict[str, Any], raw: dict[str, Any] | None
    ) -> list[dict[str, Any]]:
        content = _normalise_codex_content(item.get("content"))
        frame: dict[str, Any] = {
            "type": "agent.message",
            "role": "assistant",
            "content": content,
        }
        phase = item.get("phase")
        if isinstance(phase, str):
            frame["phase"] = phase
        if raw is not None:
            frame["raw"] = raw
        return [frame]

    # ------------------------------------------------------------------
    # `exec_command_*` (mapping pencilled in — see docs/agent-events.md)
    # ------------------------------------------------------------------

    def _translate_exec_command(
        self, msg_type: str, msg: dict[str, Any], raw: dict[str, Any] | None
    ) -> list[dict[str, Any]]:
        if msg_type == "exec_command_begin":
            tool_use_id = msg.get("call_id")
            if not isinstance(tool_use_id, str):
                return []
            input_payload = msg.get("command")
            if input_payload is None:
                input_payload = msg.get("params", {})
            frame: dict[str, Any] = {
                "type": "agent.tool_use",
                "tool_use_id": tool_use_id,
                "name": msg.get("tool") if isinstance(msg.get("tool"), str) else "shell",
                "input": input_payload,
            }
            if raw is not None:
                frame["raw"] = raw
            return [frame]
        if msg_type == "exec_command_end":
            tool_use_id = msg.get("call_id")
            if not isinstance(tool_use_id, str):
                return []
            frame = {
                "type": "agent.tool_result",
                "tool_use_id": tool_use_id,
                "output": msg.get("output"),
            }
            if "is_error" in msg:
                frame["is_error"] = bool(msg["is_error"])
            if raw is not None:
                frame["raw"] = raw
            return [frame]
        # exec_command_output_delta etc. — surface as notice for now.
        notice: dict[str, Any] = {
            "type": "agent.notice",
            "level": "info",
            "category": f"codex_{msg_type}",
        }
        if raw is not None:
            notice["raw"] = raw
        return [notice]

    # ------------------------------------------------------------------
    # Turn-end synthesis
    # ------------------------------------------------------------------

    def finalize_success(self, *, num_turns: int = 1) -> dict[str, Any]:
        """Build the terminal ``agent.result`` for a successful turn."""
        frame: dict[str, Any] = {
            "type": "agent.result",
            "subtype": "success",
            "num_turns": num_turns,
        }
        self._populate_from_task_complete(frame)
        usage = self._build_usage()
        if usage:
            frame["usage"] = usage
        self._reset_turn()
        return frame

    def finalize_error(self, error: dict[str, Any] | None, *, num_turns: int = 1) -> dict[str, Any]:
        frame: dict[str, Any] = {
            "type": "agent.result",
            "subtype": "error",
            "num_turns": num_turns,
        }
        self._populate_from_task_complete(frame)
        usage = self._build_usage()
        if usage:
            frame["usage"] = usage
        if error is not None:
            frame["error"] = error
        self._reset_turn()
        return frame

    def finalize_interrupted(self, *, num_turns: int = 1) -> dict[str, Any]:
        frame: dict[str, Any] = {
            "type": "agent.result",
            "subtype": "interrupted",
            "num_turns": num_turns,
        }
        self._populate_from_task_complete(frame)
        usage = self._build_usage()
        if usage:
            frame["usage"] = usage
        self._reset_turn()
        return frame

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _raw_for(self, msg: dict[str, Any], meta: dict[str, Any] | None) -> dict[str, Any] | None:
        if not self._include_raw:
            return None
        out = dict(msg)
        if meta is not None:
            out["_meta"] = dict(meta)
        return out

    def _populate_from_task_complete(self, frame: dict[str, Any]) -> None:
        tc = self._task_complete
        if not isinstance(tc, dict):
            return
        for key in ("duration_ms", "turn_id", "time_to_first_token_ms"):
            if key in tc and tc[key] is not None:
                frame[key] = tc[key]

    def _build_usage(self) -> dict[str, Any]:
        info = self._last_token_usage
        if not isinstance(info, dict):
            return {}
        last = info.get("last_token_usage")
        if not isinstance(last, dict):
            return {}
        out: dict[str, Any] = {}
        # Direct passthrough fields.
        for src in ("input_tokens", "output_tokens", "reasoning_output_tokens"):
            v = last.get(src)
            if isinstance(v, int):
                out[src] = v
        # Rename Codex's `cached_input_tokens` to the unified
        # `cache_read_input_tokens` so clients have a single key.
        v = last.get("cached_input_tokens")
        if isinstance(v, int):
            out["cache_read_input_tokens"] = v
        return out

    def _reset_turn(self) -> None:
        self._last_token_usage = None
        self._task_complete = None


def _normalise_codex_content(content: Any) -> list[dict[str, Any]]:
    """Convert Codex's content list (capitalised types) to the lowercase
    block shape the rest of the daemon uses.

    Codex emits ``[{"type": "Text", "text": "..."}]``; we normalise to
    ``[{"type": "text", "text": "..."}]`` so clients see the same shape
    they get from the Claude backend.
    """
    if not isinstance(content, list):
        return []
    out: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if isinstance(block_type, str) and block_type == "Text":
            text = block.get("text", "")
            out.append({"type": "text", "text": text if isinstance(text, str) else ""})
        elif isinstance(block_type, str) and block_type.lower() == "text":
            text = block.get("text", "")
            out.append({"type": "text", "text": text if isinstance(text, str) else ""})
        else:
            # Unknown / future block — pass through verbatim.
            out.append(dict(block))
    return out


def _normalise_rate_limits_codex(rate_limits: Any) -> dict[str, Any]:
    """Build the unified `agent.notice{rate_limits}.data` block from codex's
    payload.

    Shape (see ``agent-events.md`` ``rate_limits`` row + symmetry
    guarantees):

      data.limit             — primary window
      data.secondary_limit?  — secondary window (paid plans)
      data.vendor            — everything else, verbatim

    All fields under ``limit`` / ``secondary_limit`` are optional; the
    daemon does best-effort extraction.
    """
    if not isinstance(rate_limits, dict):
        return {}
    out: dict[str, Any] = {}

    primary = _extract_codex_limit(rate_limits.get("primary"))
    if primary:
        out["limit"] = primary
    secondary = _extract_codex_limit(rate_limits.get("secondary"))
    if secondary:
        out["secondary_limit"] = secondary

    vendor = {k: v for k, v in rate_limits.items() if k not in ("primary", "secondary")}
    if vendor:
        out["vendor"] = vendor
    return out


def _extract_codex_limit(block: Any) -> dict[str, Any]:
    """Pick the unified-shape fields out of one codex limit block."""
    if not isinstance(block, dict):
        return {}
    out: dict[str, Any] = {}
    resets = block.get("resets_at")
    if isinstance(resets, (int, float)):
        # Codex sends `resets_at` in Unix seconds — normalise to ms
        # to match the daemon's ms-everywhere convention (see
        # task_started.data.started_at_ms, etc).
        out["resets_at_ms"] = int(resets * 1000)
    used = block.get("used_percent")
    if isinstance(used, (int, float)):
        out["used_percent"] = used
    window = block.get("window_minutes")
    if isinstance(window, int):
        out["window_minutes"] = window
    return out


__all__ = ["CodexTranslator"]
