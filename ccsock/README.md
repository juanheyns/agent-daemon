# ccsock — Headless Claude Code Daemon

`ccsockd` is a per-user daemon that exposes the Claude Code CLI
(`claude -p`) as a long-running, multi-session backend over a Unix domain
socket. Clients get a headless Claude Code they can reach from any
language or process.

The daemon is **pass-through by design.** It injects no system prompt,
defines no tool protocol, and does not filter events; it just brokers
multiple live `claude -p` sessions, tags their events with a session id,
and forwards them. See `ccsockd-spec.md` for the authoritative spec.

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
cp packaging/ccsockd/ccsockd.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ccsockd
journalctl --user -u ccsockd -f
```

### launchd (macOS)

```bash
cp packaging/ccsockd/com.ccsock.ccsockd.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.ccsock.ccsockd.plist
```

---

## Running

```bash
ccsockd                      # foreground, socket at $XDG_RUNTIME_DIR/ccsockd.sock
ccsockd --socket /tmp/cc.s   # custom socket
ccsockd --log-level debug
```

Socket permissions are `0600`. Anyone who can `connect()` the socket has
full access to your Claude subscription, so guard it like an SSH agent.

---

## Wire protocol (summary)

Framing: newline-delimited UTF-8 JSON. Every control message has a
`ccsockd.` prefix; Claude Code events are forwarded verbatim with a
`session` field added.

Handshake:

```json
{"type":"ccsockd.hello","client":"my-tool/0.1","protocol":"ccsock/1"}
```

Open a session (all fields besides `session` optional):

```json
{
  "type": "ccsockd.open",
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
{"type":"ccsockd.user","session":"s_abc","text":"Hello"}
```

Interrupt a turn (SIGTERM → respawn with `--resume`):

```json
{"type":"ccsockd.interrupt","session":"s_abc"}
```

Close (optionally delete on-disk session state):

```json
{"type":"ccsockd.close","id":"req_99","session":"s_abc","delete":false}
```

List past sessions for a project directory (parity with interactive
`/resume`; newest first):

```json
// request
{"type":"ccsockd.list_sessions","id":"req_7","cwd":"/home/u/proj"}

// reply
{
  "type":"ccsockd.sessions","id":"req_7","cwd":"/home/u/proj",
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

Full details, flag mapping, and error codes: see `ccsockd-spec.md`.

---

## Reference client

```python
import asyncio, uuid
from ccsock.client import CcsockClient

async def main():
    async with await CcsockClient.connect() as c:
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
                if t == "stream_event":
                    inner = event.get("event", {})
                    if inner.get("type") == "content_block_delta":
                        delta = inner.get("delta", {})
                        if delta.get("type") == "text_delta":
                            print(delta["text"], end="", flush=True)
                elif t == "result":
                    print()
                    break
                elif t == "ccsockd.error":
                    raise RuntimeError(event["message"])

asyncio.run(main())
```

---

## Configuration

Precedence (high → low): CLI flag > env var > `~/.config/ccsockd/config.toml` > default.

| Key | CLI | Env | Default |
|---|---|---|---|
| `socket_path` | `--socket` | `CCSOCKD_SOCKET` | `$XDG_RUNTIME_DIR/ccsockd.sock` |
| `claude_bin` | `--claude` | `CCSOCKD_CLAUDE` | `claude` on PATH |
| `log_level` | `--log-level` | `CCSOCKD_LOG_LEVEL` | `info` |
| `log_file` | `--log-file` | `CCSOCKD_LOG_FILE` | stderr |
| `idle_timeout_s` | — | `CCSOCKD_IDLE_TIMEOUT` | `900` |
| `max_line_bytes` | — | `CCSOCKD_MAX_LINE` | `16777216` |

Example `config.toml`:

```toml
socket_path = "/tmp/my-ccsockd.sock"
log_level   = "debug"
idle_timeout_s = 300
```

---

## Testing

```bash
pip install -e ".[dev]"
pytest                              # unit + mock-claude
pytest -m requires_claude           # full E2E (needs real claude + auth)
python -m ccsock.bench --iters 3    # latency benchmark (needs running daemon)
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
