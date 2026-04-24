# blemees — Headless Claude Code Daemon

`blemeesd` is a per-user daemon that exposes the Claude Code CLI
(`claude -p`) as a long-running, multi-session backend over a Unix domain
socket. Clients get a headless Claude Code they can reach from any
language or process.

The daemon is **pass-through by design.** It injects no system prompt,
defines no tool protocol, and does not filter events; it just brokers
multiple live `claude -p` sessions, tags their events with a session id,
and forwards them. See `blemeesd-spec.md` for the authoritative spec.

---

## Install

```bash
# From source (editable, for development):
pip install -e ".[dev]"

# Minimal install:
pip install .
```

Python 3.11+ is required. No runtime dependencies outside the standard
library. The `claude` binary must be on `$PATH` (or pass `--claude`).

### systemd (Linux user unit)

```bash
mkdir -p ~/.config/systemd/user/
cp packaging/blemeesd/blemeesd.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now blemeesd
journalctl --user -u blemeesd -f
```

### launchd (macOS)

```bash
cp packaging/blemeesd/com.blemees.blemeesd.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.blemees.blemeesd.plist
```

---

## Running

```bash
blemeesd                      # foreground, socket at $XDG_RUNTIME_DIR/blemeesd.sock
blemeesd --socket /tmp/cc.s   # custom socket
blemeesd --log-level debug
```

Socket permissions are `0600`. Anyone who can `connect()` the socket has
full access to your Claude subscription, so guard it like an SSH agent.

---

## Wire protocol (summary)

Framing: newline-delimited UTF-8 JSON. Every control message has a
`blemeesd.` prefix; Claude Code events are forwarded verbatim with a
`session` field added.

Handshake:

```json
{"type":"blemeesd.hello","client":"my-tool/0.1","protocol":"blemees/1"}
```

Open a session (all fields besides `session` optional):

```json
{
  "type": "blemeesd.open",
  "id": "req_1",
  "session": "s_abc",
  "model": "sonnet",
  "system_prompt": "You are terse.",
  "tools": "",
  "permission_mode": "bypassPermissions",
  "cwd": "/home/u/proj"
}
```

Send a turn:

```json
{"type":"blemeesd.user","session":"s_abc","text":"Hello"}
```

Interrupt a turn (SIGTERM → respawn with `--resume`):

```json
{"type":"blemeesd.interrupt","session":"s_abc"}
```

Close (optionally delete on-disk session state):

```json
{"type":"blemeesd.close","id":"req_99","session":"s_abc","delete":false}
```

List past sessions for a project directory (parity with interactive
`/resume`; newest first):

```json
// request
{"type":"blemeesd.list_sessions","id":"req_7","cwd":"/home/u/proj"}

// reply
{
  "type":"blemeesd.sessions","id":"req_7","cwd":"/home/u/proj",
  "sessions":[
    {"session":"abc-123","mtime_ms":1745000000000,"size":48123,
     "attached":false,"preview":"Fix the bug in foo.py"},
    {"session":"def-456","attached":true}
  ]
}
```

`attached: true` means the daemon currently has that session running for
another connection (or your own). Records without `mtime_ms` are live
sessions whose transcript hasn't been written yet — still resumable.

### Disconnect-resilient streaming

Every outbound frame the daemon emits carries a monotonic per-session
`seq`. When a client disconnects with a turn in flight:

* The subprocess is **not killed immediately** — it runs to the next
  `result` event so the transcript closes cleanly, then exits.
* Events continue to accumulate in a per-session ring buffer (default
  1024 entries; configurable via `BLEMEESD_RING_BUFFER_SIZE`).
* Optionally they are also persisted to `event_log_dir/<session>.jsonl`
  (opt-in via `BLEMEESD_EVENT_LOG_DIR`) so replay survives daemon
  restarts.

On reconnect, the client passes `last_seen_seq` on `blemeesd.open` with
`resume: true`:

```json
{"type":"blemeesd.open","id":"r1","session":"s1","resume":true,"last_seen_seq":42}
```

The daemon replays every buffered frame with `seq > 42` before resuming
live delivery. If the ring has rolled over past the requested seq you
receive a one-shot `blemeesd.replay_gap` frame first:

```json
{"type":"blemeesd.replay_gap","session":"s1","since_seq":42,"first_available_seq":71}
```

`blemeesd.opened` carries `last_seq` (the highest seq currently known for
the session), so clients can tell immediately whether they need to
replay.

Full details, flag mapping, and error codes: see `blemeesd-spec.md`.

---

## Reference client

```python
import asyncio, uuid
from blemees.client import BlemeesClient

async def main():
    async with await BlemeesClient.connect() as c:
        async with c.open_session(
            session=str(uuid.uuid4()),
            model="sonnet",
            system_prompt="You are a terse assistant. Answer in one sentence.",
            tools="",
            permission_mode="bypassPermissions",
            cwd="/home/u/proj",
        ) as sess:
            await sess.send_user("What is 2+2?")
            async for event in sess.events():
                t = event.get("type")
                if t == "claude.stream_event":
                    inner = event.get("event", {})
                    if inner.get("type") == "content_block_delta":
                        delta = inner.get("delta", {})
                        if delta.get("type") == "text_delta":
                            print(delta["text"], end="", flush=True)
                elif t == "claude.result":
                    print()
                    break
                elif t == "blemeesd.error":
                    raise RuntimeError(event["message"])

asyncio.run(main())
```

---

## Configuration

Precedence (high → low): CLI flag > env var > `~/.config/blemeesd/config.toml` > default.

| Key | CLI | Env | Default |
|---|---|---|---|
| `socket_path` | `--socket` | `BLEMEESD_SOCKET` | `$XDG_RUNTIME_DIR/blemeesd.sock` |
| `claude_bin` | `--claude` | `BLEMEESD_CLAUDE` | `claude` on PATH |
| `log_level` | `--log-level` | `BLEMEESD_LOG_LEVEL` | `info` |
| `log_file` | `--log-file` | `BLEMEESD_LOG_FILE` | stderr |
| `idle_timeout_s` | — | `BLEMEESD_IDLE_TIMEOUT` | `900` |
| `max_line_bytes` | — | `BLEMEESD_MAX_LINE` | `16777216` |

Example `config.toml`:

```toml
socket_path = "/tmp/my-blemeesd.sock"
log_level   = "debug"
idle_timeout_s = 300
```

---

## Testing

```bash
pip install -e ".[dev]"
pytest                              # unit + mock-claude
pytest -m requires_claude           # full E2E (needs real claude + auth)
python -m blemees.bench --iters 3    # latency benchmark (needs running daemon)
```

---

## Security notes

* Socket perms are the only access control — `0600`, owned by you.
* Nothing travels over TCP; for remote access use SSH socket forwarding.
* `system_prompt`, user content, and stream deltas are never logged at
  INFO+. At DEBUG they are redacted to a character count.
* OAuth tokens are never logged.

---

## Non-goals (v0.1)

Warm subprocess pools, metrics endpoints, GUIs, Windows, remote TCP/TLS,
token refresh, tool protocols. See spec §2 and §13.
