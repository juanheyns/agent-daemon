"""Latency benchmark for blemeesd (spec §11.4).

Run with::

    python -m blemees_agent.bench [--socket PATH] [--backend claude|codex]
                             [--model MODEL] [--iters 3]

Measures three numbers per the spec:
    * cold_first_event   — open + first event, fresh session
    * warm_first_event   — second turn on the same session
    * resume_first_event — close + re-open with resume:true + first event

Acceptance targets (spec §11.4):
    * Claude: cold open → first event ≤ 1.5 s, warm user → first
      event ≤ 0.5 s, resume open → first event ≤ 1.5 s.
    * Codex: warm user → first delta ≤ 1.0 s. The cold-open cost
      includes the MCP `initialize` handshake and is documented
      empirically rather than gated.

The daemon must already be running at ``--socket``. The matching
backend's CLI must be authenticated (``claude`` for ``--backend
claude``; ``codex login status`` for ``--backend codex``) — otherwise
numbers will include auth errors.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid

from .client import BlemeesClient, default_socket_path

# `agent.*` event types that signal "model output started" — first one
# of these after a turn opens stops the latency timer.  Bookkeeping
# frames (``agent.system_init``, ``agent.notice``) are deliberately
# excluded: they fire before the upstream model has produced anything,
# so counting them would understate latency.
_FIRST_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "agent.delta",
        "agent.message",
        "agent.tool_use",
        "agent.tool_result",
        "agent.result",
    }
)

_DEFAULT_MODELS: dict[str, str] = {
    "claude": "haiku",
    "codex": "gpt-5.2-codex",
}


def _backend_options(backend: str, model: str | None) -> dict[str, object]:
    if backend == "claude":
        opts: dict[str, object] = {"tools": "", "permission_mode": "bypassPermissions"}
        if model:
            opts["model"] = model
        return opts
    if backend == "codex":
        opts = {"sandbox": "read-only", "approval-policy": "never"}
        if model:
            opts["model"] = model
        return opts
    raise SystemExit(f"unknown backend: {backend!r}")


async def _first_event_latency(sess, prompt: str) -> float:
    t0 = time.monotonic()
    await sess.send_user(prompt)
    async for evt in sess.events():
        t = evt.get("type")
        if t == "agent.error":
            raise RuntimeError(evt)
        if t in _FIRST_EVENT_TYPES:
            return time.monotonic() - t0
    raise RuntimeError("stream ended without any event")


async def _drain_to_result(sess) -> int:
    """Drain events until a turn-end ``agent.result`` arrives. Returns
    the highest seq seen so the caller can resume cleanly."""
    last_seq = 0
    async for evt in sess.events():
        seq = evt.get("seq")
        if isinstance(seq, int) and seq > last_seq:
            last_seq = seq
        if evt.get("type") == "agent.result":
            return last_seq
    return last_seq


async def run_one(
    socket_path: str, backend: str, model: str | None, prompt: str
) -> dict[str, float]:
    results: dict[str, float] = {}
    session_id = str(uuid.uuid4())
    options = _backend_options(backend, model)

    async with await BlemeesClient.connect(socket_path) as c:
        # Cold open
        t_open = time.monotonic()
        async with c.open_session(session_id=session_id, backend=backend, options=options) as sess:
            cold_latency = await _first_event_latency(sess, prompt)
            results["cold_first_event"] = cold_latency
            results["cold_open_plus_first"] = time.monotonic() - t_open
            last_seq = await _drain_to_result(sess)

            # Warm
            warm_latency = await _first_event_latency(sess, prompt)
            results["warm_first_event"] = warm_latency
            last_seq = await _drain_to_result(sess)

    # Resume: reconnect and re-open with resume:true. Codex 0.125.x's
    # `tools/call codex-reply` does not reliably rehydrate state from
    # disk across processes (returns an empty success without
    # resuming) — skip the resume step for codex until that's fixed
    # upstream so the bench doesn't hang.
    if backend == "codex":
        return results

    async with await BlemeesClient.connect(socket_path) as c:
        t_resume = time.monotonic()
        async with c.open_session(
            session_id=session_id,
            backend=backend,
            options=options,
            resume=True,
            last_seen_seq=last_seq,
        ) as sess:
            results["resume_first_event"] = await _first_event_latency(sess, prompt)
            results["resume_open_plus_first"] = time.monotonic() - t_resume
            await _drain_to_result(sess)

    return results


async def main_async(args: argparse.Namespace) -> int:
    rows: list[dict[str, float]] = []
    for i in range(args.iters):
        row = await run_one(args.socket, args.backend, args.model, args.prompt)
        rows.append(row)
        print(f"iter {i + 1}: {json.dumps(row, indent=2)}")
    if len(rows) > 1:
        keys = rows[0].keys()
        avg = {k: sum(r[k] for r in rows) / len(rows) for k in keys}
        print("average:", json.dumps(avg, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="python -m blemees_agent.bench")
    ap.add_argument("--socket", default=default_socket_path())
    ap.add_argument(
        "--backend",
        choices=("claude", "codex"),
        default="claude",
        help="Which backend to bench (default: claude)",
    )
    ap.add_argument(
        "--model",
        default=None,
        help="Backend-specific model name (default depends on --backend)",
    )
    ap.add_argument("--iters", type=int, default=3)
    ap.add_argument("--prompt", default="Reply with just the word OK.")
    args = ap.parse_args()
    if args.model is None:
        args.model = _DEFAULT_MODELS.get(args.backend)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
