---
title: blemeesd
nav_order: 1
---

# blemeesd — Headless Claude Code Daemon

A per-user daemon that exposes the Claude Code CLI (`claude -p`) as a
long-running, multi-session backend over a Unix domain socket. Clients
get a headless Claude Code they can reach from any language or process.

The daemon is **pass-through by design.** It injects no system prompt,
defines no tool protocol, and does not filter events; it just brokers
multiple live `claude -p` sessions, tags their events with a
`session_id` and `seq`, and forwards them.

---

## Get it

```bash
pip install blemees          # from PyPI
# or
brew tap juanheyns/blemees && brew install blemees
```

Start the daemon (foreground; use systemd/launchd for service mode):

```bash
blemeesd --log-level debug
```

Drive it with the reference client:

```python
import asyncio, uuid
from blemees.client import BlemeesClient

async def main():
    async with await BlemeesClient.connect() as c:
        async with c.open_session(
            session_id=str(uuid.uuid4()),
            model="sonnet",
            tools="",
            permission_mode="bypassPermissions",
        ) as sess:
            await sess.send_user("What is 2+2?")
            async for evt in sess.events():
                if evt.get("type") == "claude.result":
                    break

asyncio.run(main())
```

---

## Documentation

- **[Protocol & architecture spec](spec.html)** — the full README/spec,
  including wire format, session lifecycle, seq/ring/replay, shutdown
  semantics, and error codes.
- **[JSON Schemas](schemas.html)** — Draft 2020-12 schemas under
  `schemas/` as the machine-readable contract.
- **[GitHub repository](https://github.com/juanheyns/agent-daemon)** —
  source, issues, releases.

---

## Status

v0.1 on the `blemees/1` protocol. Pre-1.0: breaking wire changes
remain possible until the protocol is marked stable.
