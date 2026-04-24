#!/usr/bin/env python3
"""Stand-in for ``claude`` used by the mock-claude tests.

The test harness writes the desired behaviour into the ``BLEMEES_FAKE_MODE``
environment variable before spawning. Modes:

* ``normal``    → for each stdin line, emit a ``system init`` event, a
  ``stream_event`` text delta, an ``assistant`` message, then a ``result``.
* ``crash``     → consume one stdin line, write a partial event, exit with
  a non-zero code (simulates §9.1 mid-turn crash).
* ``slow``      → each stdin line starts generating; emit one text delta
  every 100 ms until SIGTERM arrives; then exit 0 (simulates an interrupt).
* ``oauth``     → emit an OAuth-style error to stderr, exit 1.
* ``echo``      → echo whatever text is in the inbound message.

The fake records its argv to ``$BLEMEES_FAKE_ARGV_FILE`` (one JSON line) so
tests can assert flag mapping.
"""

from __future__ import annotations

import json
import os
import sys
import time


def _write_argv_trace() -> None:
    path = os.environ.get("BLEMEES_FAKE_ARGV_FILE")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(sys.argv) + "\n")


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _session_id_from_argv() -> str:
    for flag in ("--session-id", "--resume"):
        if flag in sys.argv:
            idx = sys.argv.index(flag)
            if idx + 1 < len(sys.argv):
                return sys.argv[idx + 1]
    return "unknown"


def _read_user_lines():
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            yield json.loads(raw)
        except json.JSONDecodeError:
            continue


def _content_text(msg: dict) -> str:
    content = msg.get("message", {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return "".join(parts)
    return ""


def main() -> int:
    if "--version" in sys.argv:
        print("claude-fake/0.0.1")
        return 0

    _write_argv_trace()

    mode = os.environ.get("BLEMEES_FAKE_MODE", "normal")
    session = _session_id_from_argv()

    # system init event at startup
    _emit(
        {
            "type": "system",
            "subtype": "init",
            "model": "claude-fake",
            "tools": ["FakeTool"],
            "session_id": session,
        }
    )

    if mode == "oauth":
        sys.stderr.write("Error 401: OAuth token expired\n")
        sys.stderr.flush()
        return 1

    for user in _read_user_lines():
        text = _content_text(user)

        if mode == "crash":
            _emit({"type": "stream_event", "event": {"type": "content_block_start"}})
            sys.stderr.write("boom\n")
            sys.stderr.flush()
            return 2

        if mode == "slow":
            # Stream slowly until killed.
            try:
                while True:
                    _emit(
                        {
                            "type": "stream_event",
                            "event": {
                                "type": "content_block_delta",
                                "delta": {"type": "text_delta", "text": "."},
                            },
                        }
                    )
                    time.sleep(0.1)
            except KeyboardInterrupt:
                return 0
            except BrokenPipeError:
                return 0

        if mode == "finish":
            # Streams a delta, waits a configurable window, then emits a
            # natural result. Used by daemon-shutdown tests to observe the
            # "wait for turn to complete" grace-period behaviour.
            delay_s = float(os.environ.get("BLEMEES_FAKE_FINISH_DELAY_S", "0.3"))
            _emit(
                {
                    "type": "stream_event",
                    "event": {
                        "type": "content_block_delta",
                        "delta": {"type": "text_delta", "text": "."},
                    },
                }
            )
            time.sleep(delay_s)
            _emit(
                {
                    "type": "assistant",
                    "message": {"role": "assistant", "content": [{"type": "text", "text": "."}]},
                }
            )
            _emit(
                {
                    "type": "result",
                    "subtype": "success",
                    "duration_ms": int(delay_s * 1000),
                    "num_turns": 1,
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 25,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                    },
                }
            )
            continue

        # Normal / echo path.
        reply = text if mode == "echo" else f"ok:{text}"
        _emit(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": reply},
                },
            }
        )
        _emit(
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": reply}]},
            }
        )
        _emit(
            {
                "type": "result",
                "subtype": "success",
                "duration_ms": 1,
                "num_turns": 1,
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
            }
        )

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        sys.exit(0)
