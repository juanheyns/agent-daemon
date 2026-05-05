#!/usr/bin/env python3
"""Run the same prompt against both backends and write redacted, diffable transcripts.

Drives a running ``blemeesd`` over its Unix socket, opens a session on
each backend, sends the same prompt, captures every frame, and writes
two side-by-side transcripts to ``docs/traces/``. Each transcript is a
sequence of pretty-printed JSON blocks, one per wire frame, separated by
blank lines.

The redactor replaces volatile fields (timestamps, durations, IDs, token
counts, the model's actual reply text, the backend tag) with stable
placeholders so a plain ``diff`` highlights only *structural* and
*naming* differences between the two streams.

Usage::

    blemeesd &                                    # daemon must be running
    python scripts/transcript_compare.py          # default prompt
    python scripts/transcript_compare.py --prompt "Reply with: ok"
    diff -u docs/traces/transcript-claude.txt docs/traces/transcript-codex.txt

The daemon must have *both* backends available (``claude`` and
``codex`` on ``$PATH``, both authenticated). Each turn consumes a
small amount of credits on both upstreams.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "docs" / "traces"
DEFAULT_PROMPT = "Reply with exactly: pong"


# ---------------------------------------------------------------------------
# Wire driver
# ---------------------------------------------------------------------------


def _default_socket() -> str:
    env = os.environ.get("BLEMEESD_SOCKET")
    if env:
        return env
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return str(Path(xdg) / "agent.sock")
    return f"/tmp/blemeesd-{os.getuid()}.sock"


async def _send(writer: asyncio.StreamWriter, frame: dict[str, Any]) -> None:
    line = (json.dumps(frame, ensure_ascii=False) + "\n").encode("utf-8")
    writer.write(line)
    await writer.drain()


async def _recv(reader: asyncio.StreamReader, timeout: float) -> dict[str, Any]:
    raw = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=timeout)
    return json.loads(raw.rstrip(b"\r\n").decode("utf-8"))


async def capture(
    socket_path: str,
    *,
    backend: str,
    options: dict[str, Any],
    prompt: str,
    open_timeout: float,
    turn_timeout: float,
) -> list[dict[str, Any]]:
    """Open one session, run one turn, close it. Return every frame seen."""
    reader, writer = await asyncio.open_unix_connection(socket_path)
    frames: list[dict[str, Any]] = []
    try:
        await _send(
            writer,
            {
                "type": "agent.hello",
                "client": "transcript-compare/0",
                "protocol": "blemees/2",
            },
        )
        frames.append(await _recv(reader, open_timeout))  # hello_ack

        session_id = str(uuid.uuid4())
        await _send(
            writer,
            {
                "type": "agent.open",
                "id": "open-1",
                "session_id": session_id,
                "backend": backend,
                "options": {backend: options},
            },
        )
        # Drain until the agent.opened ack (or an error).
        while True:
            evt = await _recv(reader, open_timeout)
            frames.append(evt)
            t = evt.get("type")
            if t == "agent.opened":
                break
            if t == "agent.error":
                raise RuntimeError(f"open failed: {evt}")

        await _send(
            writer,
            {
                "type": "agent.user",
                "session_id": session_id,
                "message": {"role": "user", "content": prompt},
            },
        )
        while True:
            evt = await _recv(reader, turn_timeout)
            frames.append(evt)
            if evt.get("type") == "agent.result" and evt.get("session_id") == session_id:
                break

        await _send(
            writer,
            {
                "type": "agent.close",
                "id": "close-1",
                "session_id": session_id,
                "delete": True,
            },
        )
        while True:
            evt = await _recv(reader, open_timeout)
            frames.append(evt)
            if evt.get("type") == "agent.closed":
                break
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass
    return frames


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


# Leaf-level fields whose value is replaced by a fixed placeholder. Keeps
# the field present so structural shape (which fields exist) still diffs.
_FIXED_REDACTIONS: dict[str, str] = {
    "session_id": "<session_id>",
    "id": "<req_id>",
    "pid": "<pid>",
    "subprocess_pid": "<pid>",
    "daemon": "<daemon_version>",
    "model": "<model>",
    "cwd": "<cwd>",
    "rollout_path": "<rollout_path>",
    "started_at_ms": "<ts>",
    "started_at": "<ts>",
    "last_turn_at_ms": "<ts>",
    "resets_at_ms": "<ts>",
    "resets_at": "<ts>",
    "resetsAt": "<ts>",
    "duration_ms": "<ms>",
    "time_to_first_token_ms": "<ms>",
    # Volatile gauges that change every run; redact so structural
    # diffs aren't drowned in numeric churn.
    "used_percent": "<percent>",
    # Reply text we don't care about for shape-diff.
    "text": "<text>",
    "delta": "<text>",
    "partial_json": "<partial_json>",
    "thinking": "<thinking>",
    # Tool-related opaque payloads.
    "input": "<input>",
    "output": "<output>",
    "command": "<command>",
    # `agent.stderr.line` is free-form diagnostic text from the
    # backend child — useful at runtime, but unstable across runs
    # (timestamps, paths, PIDs) so it dominates a structural diff.
    "line": "<stderr_line>",
}

# Leaf-level fields that get *enumerated* per transcript: same input
# value within a single transcript maps to the same placeholder, so
# back-references are visible (`<tool_use_id_1>` appears in both the
# tool_use and tool_result that pair up).
_ENUMERATED_LEAVES: tuple[str, ...] = (
    "native_session_id",
    "turn_id",
    "item_id",
    "tool_use_id",
    "call_id",
    "threadId",
    "thread_id",
    "requestId",
)


class Redactor:
    """Per-transcript stable redactor.

    Two redactors built for the two backends will produce comparable
    placeholder names when the underlying *positions* match — i.e. the
    first turn_id seen on each side becomes ``<turn_id_1>`` regardless
    of the actual UUID. That keeps the diff focused on shape, not IDs.
    """

    def __init__(self) -> None:
        self._counters: dict[str, int] = defaultdict(int)
        self._cache: dict[tuple[str, Any], str] = {}

    def redact(self, frame: dict[str, Any]) -> dict[str, Any]:
        # Strip top-level `backend` and `seq` from output: `backend` is
        # the very thing that differs by design, and `seq` is implicit
        # from line position in the transcript file.
        cleaned = {k: v for k, v in frame.items() if k not in {"backend", "seq"}}
        return self._walk(cleaned, path=())

    def _walk(self, value: Any, *, path: tuple[str, ...]) -> Any:
        if isinstance(value, dict):
            return {k: self._walk(v, path=path + (k,)) for k, v in value.items()}
        if isinstance(value, list):
            return [self._walk(v, path=path) for v in value]
        return self._leaf(value, path=path)

    def _leaf(self, value: Any, *, path: tuple[str, ...]) -> Any:
        leaf = path[-1] if path else ""
        if leaf in _FIXED_REDACTIONS:
            return _FIXED_REDACTIONS[leaf]
        if leaf in _ENUMERATED_LEAVES:
            return self._enum(leaf, value)
        # Token counts: any integer under a `usage` or `last_turn_usage`
        # / `cumulative_usage` ancestor.
        if isinstance(value, int) and any(
            p in {"usage", "last_turn_usage", "cumulative_usage"} for p in path
        ):
            return "<int>"
        return value

    def _enum(self, kind: str, value: Any) -> str:
        key = (kind, value)
        if key in self._cache:
            return self._cache[key]
        self._counters[kind] += 1
        placeholder = f"<{kind}_{self._counters[kind]}>"
        self._cache[key] = placeholder
        return placeholder


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def write_transcript(path: Path, frames: list[dict[str, Any]], *, header: list[str]) -> None:
    redactor = Redactor()
    blocks: list[str] = []
    for line in header:
        blocks.append(f"# {line}")
    blocks.append("")  # blank line after header

    for frame in frames:
        redacted = redactor.redact(frame)
        # sort_keys=True so a missing key on one side and a renamed key
        # on the other both pop out in the diff at the same line offset.
        blocks.append(json.dumps(redacted, indent=2, sort_keys=True))
        blocks.append("")  # blank line between frames

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(blocks).rstrip() + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


# Minimal, roughly-equivalent option blocks. The point is to keep both
# backends restrictive (no tool use, no shell access) so the transcripts
# focus on the conversational shape rather than tool-call traffic.
DEFAULT_CLAUDE_OPTIONS: dict[str, Any] = {
    "tools": "",
    "permission_mode": "bypassPermissions",
}
DEFAULT_CODEX_OPTIONS: dict[str, Any] = {
    "sandbox": "read-only",
    "approval-policy": "never",
}


async def _amain(args: argparse.Namespace) -> int:
    socket_path = args.socket or _default_socket()
    if not Path(socket_path).exists():
        print(
            f"error: blemeesd socket not found at {socket_path}.\n"
            "Start the daemon first (`blemeesd &`) or pass --socket.",
            file=sys.stderr,
        )
        return 2

    out_dir = Path(args.out)
    header_common = [
        f"prompt: {args.prompt!r}",
        "redacted: timestamps, durations, IDs, token counts, model reply text, "
        "and the top-level `backend`/`seq` fields are normalised so a plain "
        "`diff` highlights only structural / naming differences.",
        "generator: scripts/transcript_compare.py",
    ]

    try:
        claude_frames = await capture(
            socket_path,
            backend="claude",
            options=DEFAULT_CLAUDE_OPTIONS,
            prompt=args.prompt,
            open_timeout=args.open_timeout,
            turn_timeout=args.turn_timeout,
        )
    except Exception as exc:  # noqa: BLE001 — script-level diagnostic
        print(f"error: claude capture failed: {exc}", file=sys.stderr)
        return 3

    try:
        codex_frames = await capture(
            socket_path,
            backend="codex",
            options=DEFAULT_CODEX_OPTIONS,
            prompt=args.prompt,
            open_timeout=args.open_timeout,
            turn_timeout=args.turn_timeout,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"error: codex capture failed: {exc}", file=sys.stderr)
        return 3

    claude_path = out_dir / "transcript-claude.txt"
    codex_path = out_dir / "transcript-codex.txt"
    write_transcript(claude_path, claude_frames, header=["backend: claude"] + header_common)
    write_transcript(codex_path, codex_frames, header=["backend: codex"] + header_common)

    print(f"wrote {claude_path} ({len(claude_frames)} frames)")
    print(f"wrote {codex_path} ({len(codex_frames)} frames)")
    print(f"\ndiff -u {claude_path} {codex_path}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--prompt",
        default=DEFAULT_PROMPT,
        help=f"prompt sent to both backends (default: {DEFAULT_PROMPT!r})",
    )
    parser.add_argument(
        "--socket",
        default=None,
        help="blemeesd socket path (default: $BLEMEESD_SOCKET / "
        "$XDG_RUNTIME_DIR/agent.sock / /tmp/blemeesd-<uid>.sock)",
    )
    parser.add_argument(
        "--out",
        default=str(DEFAULT_OUT),
        help=f"output directory for the two transcripts (default: {DEFAULT_OUT})",
    )
    parser.add_argument(
        "--open-timeout",
        type=float,
        default=30.0,
        help="seconds to wait for agent.opened / agent.closed (default: 30)",
    )
    parser.add_argument(
        "--turn-timeout",
        type=float,
        default=120.0,
        help="seconds to wait for the turn's agent.result (default: 120)",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(_amain(args)))


if __name__ == "__main__":
    main()
