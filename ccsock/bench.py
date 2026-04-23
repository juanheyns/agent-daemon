"""Latency benchmark for ccsockd (spec §11.4).

Run with::

    python -m ccsock.bench [--socket PATH] [--model haiku] [--iters 3]

Measures three numbers per the spec:
    * cold_first_event   — open + first event, fresh session
    * warm_first_event   — second turn on the same session
    * resume_first_event — close + re-open with resume:true + first event

The daemon must already be running at ``--socket``. The ``claude`` binary
must be authenticated (otherwise numbers will include OAuth errors).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
import uuid

from . import PROTOCOL_VERSION
from .client import CcsockClient, default_socket_path


async def _first_event_latency(sess, prompt: str) -> float:
    t0 = time.monotonic()
    await sess.send_user(prompt)
    async for evt in sess.events():
        t = evt.get("type")
        if t == "ccsockd.error":
            raise RuntimeError(evt)
        if t in {"stream_event", "assistant", "partial_assistant", "result"}:
            return time.monotonic() - t0
    raise RuntimeError("stream ended without any event")


async def _drain_to_result(sess) -> None:
    async for evt in sess.events():
        if evt.get("type") == "result":
            return


async def run_one(socket_path: str, model: str, prompt: str) -> dict[str, float]:
    results: dict[str, float] = {}
    session_id = str(uuid.uuid4())

    async with await CcsockClient.connect(socket_path) as c:
        # Cold open
        t_open = time.monotonic()
        async with c.open_session(
            session=session_id, model=model, tools="", permission_mode="bypassPermissions"
        ) as sess:
            cold_latency = await _first_event_latency(sess, prompt)
            results["cold_first_event"] = cold_latency
            results["cold_open_plus_first"] = time.monotonic() - t_open
            await _drain_to_result(sess)

            # Warm
            warm_latency = await _first_event_latency(sess, prompt)
            results["warm_first_event"] = warm_latency
            await _drain_to_result(sess)

    # Resume: reconnect and re-open with resume:true.
    async with await CcsockClient.connect(socket_path) as c:
        t_resume = time.monotonic()
        async with c.open_session(
            session=session_id,
            model=model,
            tools="",
            resume=True,
            permission_mode="bypassPermissions",
        ) as sess:
            results["resume_first_event"] = await _first_event_latency(sess, prompt)
            results["resume_open_plus_first"] = time.monotonic() - t_resume
            await _drain_to_result(sess)

    return results


async def main_async(args: argparse.Namespace) -> int:
    rows: list[dict[str, float]] = []
    for i in range(args.iters):
        row = await run_one(args.socket, args.model, args.prompt)
        rows.append(row)
        print(f"iter {i + 1}: {json.dumps(row, indent=2)}")
    if len(rows) > 1:
        keys = rows[0].keys()
        avg = {k: sum(r[k] for r in rows) / len(rows) for k in keys}
        print("average:", json.dumps(avg, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="python -m ccsock.bench")
    ap.add_argument("--socket", default=default_socket_path())
    ap.add_argument("--model", default="haiku")
    ap.add_argument("--iters", type=int, default=3)
    ap.add_argument("--prompt", default="Reply with just the word OK.")
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
