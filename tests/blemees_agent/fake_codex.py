#!/usr/bin/env python3
"""Stand-in for ``codex mcp-server`` used by the mock-codex tests.

Speaks JSON-RPC 2.0 over NDJSON stdio. The mode is selected via
``BLEMEES_FAKE_MODE`` (mirrors ``fake_claude.py``):

* ``normal`` — handshake, then for each ``tools/call`` emit a
  ``session_configured`` (first turn only), an ``agent_message_content_delta``,
  an ``item_completed{AgentMessage}``, a final ``token_count``, a
  ``task_complete``, and a JSON-RPC ``result``.
* ``crash`` — handshake, then on first ``tools/call`` write a partial
  ``codex/event`` line and exit non-zero.
* ``auth`` — handshake, then on first ``tools/call`` reply with a
  JSON-RPC ``error`` whose code/message look auth-related.
* ``slow`` — handshake, then on each ``tools/call`` emit
  ``session_configured`` + a stream of deltas every 50 ms until
  ``notifications/cancelled`` arrives, after which we send the JSON-RPC
  result.
* ``echo`` — like ``normal`` but the assistant content echoes the
  prompt verbatim.

Argv is recorded to ``$BLEMEES_FAKE_ARGV_FILE`` so tests can assert
spawn-time flag mapping.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid

THREAD_ID = "fake-thread-0000-1111-2222-333333333333"


def _write_argv_trace() -> None:
    path = os.environ.get("BLEMEES_FAKE_ARGV_FILE")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(sys.argv) + "\n")


def _log_rpc(record: dict) -> None:
    path = os.environ.get("BLEMEES_FAKE_RPC_LOG")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError:
        pass


_emit_lock = threading.Lock()


def _emit(obj: dict) -> None:
    line = json.dumps(obj) + "\n"
    with _emit_lock:
        sys.stdout.write(line)
        sys.stdout.flush()


def _emit_raw(line: str) -> None:
    with _emit_lock:
        sys.stdout.write(line)
        sys.stdout.flush()


def _read_lines():
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            yield json.loads(raw)
        except json.JSONDecodeError:
            continue


def _tools_payload() -> dict:
    return {
        "tools": [
            {
                "name": "codex",
                "title": "Codex",
                "description": "Run a Codex session.",
                "inputSchema": {"type": "object", "properties": {"prompt": {"type": "string"}}},
                "outputSchema": {
                    "type": "object",
                    "properties": {
                        "threadId": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["threadId", "content"],
                },
            },
            {
                "name": "codex-reply",
                "title": "Codex Reply",
                "description": "Continue a Codex conversation.",
                "inputSchema": {"type": "object", "properties": {"prompt": {"type": "string"}}},
                "outputSchema": {
                    "type": "object",
                    "properties": {
                        "threadId": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["threadId", "content"],
                },
            },
        ]
    }


def _emit_event(thread_id: str, request_id: int, msg: dict) -> None:
    _emit(
        {
            "jsonrpc": "2.0",
            "method": "codex/event",
            "params": {
                "_meta": {"requestId": request_id, "threadId": thread_id},
                "id": str(request_id),
                "msg": msg,
            },
        }
    )


def _send_session_configured(thread_id: str, request_id: int) -> None:
    _emit_event(
        thread_id,
        request_id,
        {
            "type": "session_configured",
            "session_id": thread_id,
            "model": "fake-codex",
            "model_provider_id": "fake",
            "approval_policy": "never",
            "sandbox_policy": {"type": "read-only"},
            "permission_profile": {"type": "managed"},
            "cwd": os.getcwd(),
            "reasoning_effort": "low",
            "rollout_path": "/tmp/fake-rollout.jsonl",
        },
    )


def _send_task_started(thread_id: str, request_id: int, turn_id: str) -> None:
    _emit_event(
        thread_id,
        request_id,
        {
            "type": "task_started",
            "turn_id": turn_id,
            "started_at": int(time.time()),
            "model_context_window": 200000,
        },
    )


def _send_task_complete(
    thread_id: str, request_id: int, turn_id: str, *, duration_ms: int = 5
) -> None:
    _emit_event(
        thread_id,
        request_id,
        {
            "type": "task_complete",
            "turn_id": turn_id,
            "duration_ms": duration_ms,
            "time_to_first_token_ms": 1,
        },
    )


def _send_token_count(thread_id: str, request_id: int) -> None:
    _emit_event(
        thread_id,
        request_id,
        {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "input_tokens": 10,
                    "cached_input_tokens": 4,
                    "output_tokens": 5,
                    "reasoning_output_tokens": 2,
                    "total_tokens": 17,
                },
                "last_token_usage": {
                    "input_tokens": 10,
                    "cached_input_tokens": 4,
                    "output_tokens": 5,
                    "reasoning_output_tokens": 2,
                    "total_tokens": 17,
                },
                "model_context_window": 200000,
            },
            "rate_limits": {},
        },
    )


def _send_delta(thread_id: str, request_id: int, item_id: str, text: str) -> None:
    _emit_event(
        thread_id,
        request_id,
        {
            "type": "agent_message_content_delta",
            "item_id": item_id,
            "delta": text,
        },
    )


def _send_user_message_completed(thread_id: str, request_id: int, prompt: str) -> None:
    """Mirror real codex's `item_completed{UserMessage}` event for the
    user's input message — used to exercise the daemon's `user_echo`
    suppression toggle."""
    _emit_event(
        thread_id,
        request_id,
        {
            "type": "item_completed",
            "thread_id": thread_id,
            "turn_id": str(request_id),
            "item": {
                "type": "UserMessage",
                "id": f"u_{uuid.uuid4().hex[:12]}",
                "content": [{"type": "Text", "text": prompt}],
            },
        },
    )


def _send_agent_message_completed(thread_id: str, request_id: int, item_id: str, text: str) -> None:
    _emit_event(
        thread_id,
        request_id,
        {
            "type": "item_completed",
            "thread_id": thread_id,
            "turn_id": str(request_id),
            "item": {
                "type": "AgentMessage",
                "id": item_id,
                "content": [{"type": "Text", "text": text}],
                "phase": "final_answer",
            },
        },
    )


def _content_text(args: dict) -> str:
    """Pull a prompt out of `tools/call` arguments — matches what the
    backend builder synthesises (single ``prompt`` string)."""
    if not isinstance(args, dict):
        return ""
    prompt = args.get("prompt", "")
    if isinstance(prompt, str):
        return prompt
    return ""


def _run_normal_turn(
    thread_id: str, request_id: int, prompt: str, *, first_turn: bool, mode: str
) -> None:
    if first_turn:
        _send_session_configured(thread_id, request_id)
    _send_task_started(thread_id, request_id, str(request_id))
    _send_user_message_completed(thread_id, request_id, prompt)
    item_id = f"msg_{uuid.uuid4().hex[:12]}"
    reply = prompt if mode == "echo" else f"ok:{prompt}"
    # Stream the reply in two halves to match real Codex behaviour.
    half = max(1, len(reply) // 2) if reply else 1
    if reply:
        _send_delta(thread_id, request_id, item_id, reply[:half])
        if len(reply) > half:
            _send_delta(thread_id, request_id, item_id, reply[half:])
    else:
        _send_delta(thread_id, request_id, item_id, "")
    _send_agent_message_completed(thread_id, request_id, item_id, reply)
    _send_token_count(thread_id, request_id)
    _send_task_complete(thread_id, request_id, str(request_id))
    _emit(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [{"type": "text", "text": reply}],
                "structuredContent": {"threadId": thread_id, "content": reply},
            },
        }
    )


_cancel_event = threading.Event()
_cancel_request_id: int | None = None


def _run_slow_turn(thread_id: str, request_id: int) -> None:
    global _cancel_request_id
    _cancel_request_id = request_id
    _cancel_event.clear()
    _send_session_configured(thread_id, request_id)
    _send_task_started(thread_id, request_id, str(request_id))
    item_id = f"msg_{uuid.uuid4().hex[:12]}"
    while not _cancel_event.is_set():
        _send_delta(thread_id, request_id, item_id, ".")
        time.sleep(0.05)
    # Send the final response so the daemon can finalize the turn.
    _emit(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [{"type": "text", "text": ""}],
                "structuredContent": {"threadId": thread_id, "content": ""},
            },
        }
    )


def main() -> int:
    if "--version" in sys.argv:
        print("codex-cli 0.0.1")
        return 0

    _write_argv_trace()

    mode = os.environ.get("BLEMEES_FAKE_MODE", "normal")
    thread_id = os.environ.get("BLEMEES_FAKE_THREAD_ID", THREAD_ID)
    first_turn = True

    for obj in _read_lines():
        if not isinstance(obj, dict):
            continue

        method = obj.get("method")
        msg_id = obj.get("id")

        if method == "initialize":
            _emit(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {"listChanged": True}},
                        "serverInfo": {"name": "fake-codex", "version": "0.0.1"},
                    },
                }
            )
            continue
        if method == "notifications/initialized":
            continue
        if method == "tools/list":
            _emit({"jsonrpc": "2.0", "id": msg_id, "result": _tools_payload()})
            continue
        if method == "notifications/cancelled":
            _cancel_event.set()
            continue
        if method == "tools/call":
            params = obj.get("params") or {}
            args = params.get("arguments") or {}
            prompt = _content_text(args)
            _log_rpc(
                {
                    "tool": params.get("name"),
                    "thread_id": args.get("threadId"),
                    "prompt": prompt,
                }
            )

            if mode == "crash":
                # Emit a half-line and abort — exercises the crash path.
                _emit_raw('{"jsonrpc":"2.0","method":"codex/event","params":{"_meta":')
                sys.stdout.flush()
                sys.stderr.write("codex fake: crash\n")
                sys.stderr.flush()
                return 2

            if mode == "auth":
                _emit(
                    {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "error": {
                            "code": -32001,
                            "message": "401 Unauthorized: please run `codex login`",
                        },
                    }
                )
                continue

            if mode == "slow":
                if not isinstance(msg_id, int):
                    continue
                t = threading.Thread(target=_run_slow_turn, args=(thread_id, msg_id), daemon=True)
                t.start()
                continue

            if not isinstance(msg_id, int):
                continue
            _run_normal_turn(thread_id, msg_id, prompt, first_turn=first_turn, mode=mode)
            first_turn = False
            continue

        # Unknown — ignore.
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
