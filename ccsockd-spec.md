# ccsockd — Headless Claude Code Daemon

**Version:** 0.1 (draft, pre-implementation)
**Status:** Ready for an implementing agent to build from zero.
**Language:** Python 3.11+, stdlib only (no runtime deps). Type-hinted.
**Target OS:** Linux, macOS. Windows not supported in v0.1.

---

## 1. Overview

`ccsockd` is a per-user daemon that exposes the Claude Code CLI (`claude -p`)
as a long-running, multi-session backend over a Unix domain socket. It is a
thin, general-purpose wrapper: clients get a headless Claude Code they can
reach from any language, any process.

The daemon is **pass-through by design.** It does not inject a system prompt,
does not implement a tool protocol, does not filter events. It:

1. Listens on a Unix socket.
2. Lets clients open, drive, interrupt, resume, and close Claude Code sessions.
3. Forwards Claude Code's `stream-json` events to the client with a `session`
   field added.
4. Manages subprocess lifecycle (spawn, kill, respawn via `--resume`).

---

## 2. Goals and Non-Goals

### Goals (v0.1)
- Expose `claude -p` over a local Unix socket, multiplexing multiple sessions.
- Support the full `claude -p` flag surface relevant to non-interactive use
  (§6.1). Clients control their own system prompt, tools, model, effort, cwd,
  MCP config, etc.
- Session resume across client disconnects and daemon restarts via
  `--resume <session-id>`.
- Interrupt: kill the in-flight turn cleanly and allow continuation.
- Sub-second warm first-event latency; ~1 s cold start.
- Be **neutral** — no client-specific assumptions, no built-in prompts, no
  tool protocols, no post-processing of events beyond session tagging.

### Non-goals (v0.1)
- Inventing a tool protocol. Clients either use Claude Code's native tools
  (via `--tools`, `--mcp-config`, etc.) or implement their own protocol in
  their own system prompt. The daemon does not parse assistant output.
- Multi-user daemons. One `ccsockd` per OS user. Socket perms (0600) are the
  only access control.
- Remote access (TCP/TLS). Use SSH socket forwarding if needed.
- Running `claude` interactively (without `-p`).
- Token refresh. If OAuth expires, surface the error and let the user run
  `claude auth` manually.
- Prompt caching control, token accounting, GUI/admin interface.

---

## 3. Architecture

```
┌──────────────────────────────────────────────────────┐
│ ccsockd (single asyncio event loop)                  │
│                                                      │
│   UnixServer  listens on $XDG_RUNTIME_DIR/ccsockd.sock
│      │                                               │
│      ├─ Connection 1                                 │
│      │    ├─ Session s_abc  → Subprocess A (sonnet) │
│      │    └─ Session s_def  → Subprocess B (opus)   │
│      │                                               │
│      └─ Connection 2                                 │
│           └─ Session s_xyz  → Subprocess C (haiku)  │
│                                                      │
│   SubprocessManager                                  │
│     - spawns/kills/respawns `claude -p` children     │
│                                                      │
│   SessionTable                                       │
│     - session_id → (connection_id?, subprocess, cwd)│
│     - reaps orphans after IDLE_TIMEOUT              │
└──────────────────────────────────────────────────────┘
```

- Single asyncio event loop. `asyncio.subprocess` handles stdio.
- One `claude -p` subprocess per open session.
- Sessions outlive client connections (reattach via `resume: true`).
- Unattached sessions reaped after `IDLE_TIMEOUT` (default 900 s).

---

## 4. File Layout

```
ccsock/
  __init__.py
  __main__.py       # python -m ccsock → daemon entry point
  daemon.py         # UnixServer + connection dispatcher
  protocol.py       # wire protocol codec, message dataclasses
  session.py        # SessionTable
  subprocess.py     # ClaudeSubprocess wrapper (spawn, stream, kill, resume)
  config.py         # config loading (file + env + CLI)
  errors.py         # typed exceptions
  logging.py        # structured logging helpers
  client.py         # reference Python client (~200 lines, stdlib only)
tests/ccsock/
  test_protocol.py
  test_session.py
  test_subprocess.py
  test_daemon_mock.py  # mock `claude` stub
  test_daemon_e2e.py   # requires real `claude`, gated
```

Package is self-contained (no external imports outside stdlib). A console
script `ccsockd` in `pyproject.toml` maps to `python -m ccsock`.

---

## 5. Wire Protocol

### 5.1 Framing

- Transport: `AF_UNIX` stream socket.
- Framing: UTF-8 newline-delimited JSON. Exactly one JSON object per line.
- Max line size: 16 MiB (configurable). Oversize → connection closed with an
  `error` frame.
- Full duplex. Neither side should block on write (see §9.3).

### 5.2 Message namespacing

All daemon control messages use a `ccsockd.` prefix on their `type`. Events
originating inside Claude Code keep their native `type` unchanged, plus a
`session` field injected at the top level.

| Direction | Prefix | Purpose |
|---|---|---|
| client ↔ daemon | `ccsockd.<verb>` | Control messages (hello, open, user, interrupt, close, error, …). |
| daemon → client | (native CC `type`, no prefix) | Pass-through Claude Code events with `session` injected. |

Rationale: unambiguous separation between daemon-level and CC-level events.
Clients can switch-case on `type` without worrying about collisions.

### 5.3 Handshake

Client opens the connection and sends:
```json
{"type":"ccsockd.hello","client":"your-tool/0.1","protocol":"ccsock/1"}
```
Daemon replies:
```json
{"type":"ccsockd.hello_ack","daemon":"ccsockd/0.1","protocol":"ccsock/1","pid":12345,"claude_version":"2.1.118"}
```
If `protocol` does not match, daemon sends `ccsockd.error` (code
`protocol_mismatch`) and closes.

### 5.4 Session open

Client supplies whichever `claude -p` flags it wants. All fields except
`session` are OPTIONAL; the daemon omits corresponding flags when unset,
letting Claude Code apply its defaults.

```json
{
  "type": "ccsockd.open",
  "id": "req_001",
  "session": "s_abc",

  "model": "sonnet",
  "system_prompt": "...",
  "append_system_prompt": "...",
  "tools": "default",
  "disallowed_tools": [],
  "permission_mode": "default",
  "cwd": "/home/u/proj",
  "add_dir": ["/home/u/proj/vendored"],
  "effort": "medium",
  "agent": null,
  "agents": null,
  "mcp_config": [],
  "strict_mcp_config": false,
  "settings": null,
  "setting_sources": null,
  "plugin_dir": [],
  "betas": [],
  "exclude_dynamic_system_prompt_sections": false,
  "max_budget_usd": null,
  "json_schema": null,
  "fallback_model": null,
  "session_name": null,
  "session_persistence": true,
  "input_format": "stream-json",
  "output_format": "stream-json",
  "include_partial_messages": true,
  "replay_user_messages": false,

  "resume": false
}
```

Daemon flag mapping (only fields set by the client produce a flag; unset
fields are omitted):

| Field | CLI flag |
|---|---|
| `model` | `--model <v>` |
| `system_prompt` | `--system-prompt <v>` |
| `append_system_prompt` | `--append-system-prompt <v>` |
| `tools` | `--tools <v>` (use `""` to disable all) |
| `disallowed_tools` | `--disallowedTools <v...>` |
| `permission_mode` | `--permission-mode <v>` |
| `cwd` | `chdir()` before spawn |
| `add_dir` | `--add-dir <v...>` |
| `effort` | `--effort <v>` |
| `agent` | `--agent <v>` |
| `agents` | `--agents <json>` |
| `mcp_config` | `--mcp-config <v...>` |
| `strict_mcp_config` | `--strict-mcp-config` |
| `settings` | `--settings <v>` |
| `setting_sources` | `--setting-sources <v>` |
| `plugin_dir` | `--plugin-dir <v>` (repeated) |
| `betas` | `--betas <v...>` |
| `exclude_dynamic_system_prompt_sections` | `--exclude-dynamic-system-prompt-sections` |
| `max_budget_usd` | `--max-budget-usd <v>` |
| `json_schema` | `--json-schema <v>` |
| `fallback_model` | `--fallback-model <v>` |
| `session_name` | `-n <v>` |
| `session_persistence` | `--no-session-persistence` when `false` |
| `input_format` | `--input-format <v>` (default: `stream-json`) |
| `output_format` | `--output-format <v>` (default: `stream-json`) |
| `include_partial_messages` | `--include-partial-messages` |
| `replay_user_messages` | `--replay-user-messages` |
| `session` + `resume:true` | `--resume <session>` |
| `session` + `resume:false` | `--session-id <session>` |

Flags the daemon refuses to pass (always rejected with `unsafe_flag`):
`--dangerously-skip-permissions`, `--allow-dangerously-skip-permissions`,
`--bare` (see note), `--continue`, `--from-pr`. Clients that need
bypassPermissions should pass `"permission_mode":"bypassPermissions"`
explicitly — the daemon allows that, it just refuses the legacy kill switch.

> **`--bare` note:** bare mode disables OAuth/keychain auth and requires
> `ANTHROPIC_API_KEY`. Incompatible with the daemon's typical auth
> assumption. v0.1 does not support it.

Daemon always enforces `--verbose` (required when `--output-format stream-json`
is used with `-p`). Clients cannot override.

Daemon reply on success:
```json
{"type":"ccsockd.opened","id":"req_001","session":"s_abc","subprocess_pid":54321}
```
On failure:
```json
{"type":"ccsockd.error","id":"req_001","session":"s_abc","code":"spawn_failed","message":"..."}
```

### 5.5 User message

Client sends a new user turn to an open session:
```json
{"type":"ccsockd.user","session":"s_abc","text":"Hello"}
```

For multimodal / richer content, clients pass an explicit `content` array
using Claude Code's stream-json input schema directly:
```json
{"type":"ccsockd.user","session":"s_abc","content":[{"type":"text","text":"What is in this image?"},{"type":"image","source":{"type":"base64","media_type":"image/png","data":"..."}}]}
```
If `content` is present, `text` is ignored.

No `id` required. Responses stream as events until the turn ends.

### 5.6 Event stream (daemon → client)

The daemon reads each line of `claude -p` stdout, parses as JSON, injects
`"session":"<id>"`, and forwards verbatim. Clients consume Claude Code's
native event shapes (`system`, `stream_event`, `assistant`, `user`, `result`,
`partial_assistant`, etc.).

Example (abridged):
```json
{"session":"s_abc","type":"system","subtype":"init","model":"claude-sonnet-4-6","tools":["Bash","Read","Edit"]}
{"session":"s_abc","type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":"Hel"}}}
{"session":"s_abc","type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":"lo"}}}
{"session":"s_abc","type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"Hello"}]}}
{"session":"s_abc","type":"result","subtype":"success","duration_ms":1254,"num_turns":1}
```

The daemon does NOT translate, filter, deduplicate, or re-shape these events.
Clients that only want streaming deltas should ignore the final `assistant`
echo to avoid double-counting.

Events that arrive on the subprocess's **stderr** are wrapped and forwarded
as well (for visibility into CC warnings / auth errors):
```json
{"session":"s_abc","type":"ccsockd.stderr","line":"..."}
```
These are rate-limited to prevent a broken subprocess from flooding the
client. Default cap: 50 lines per 10 s; excess dropped with a counter.

### 5.7 Interrupt

Client cancels the in-flight turn:
```json
{"type":"ccsockd.interrupt","session":"s_abc"}
```

Daemon:
1. Sends SIGTERM to the subprocess. After 500 ms, SIGKILL if still alive.
2. Emits `ccsockd.interrupted`:
   ```json
   {"type":"ccsockd.interrupted","session":"s_abc"}
   ```
3. Respawns the subprocess immediately with `--resume <session>` (all other
   flags identical to the original open), so the next `ccsockd.user` works
   without further ceremony.

Any CC events emitted before the kill are forwarded as normal. Already-sent
deltas are NOT retracted.

Interrupt is a no-op (returns `ccsockd.interrupted` with `was_idle: true`) if
no turn is in flight.

### 5.8 Close

Explicit session close:
```json
{"type":"ccsockd.close","id":"req_099","session":"s_abc","delete":true}
```
- `delete: true` → daemon removes the CC session file from disk after kill.
- `delete: false` (default) → session file retained for later `resume: true`.

Daemon replies:
```json
{"type":"ccsockd.closed","id":"req_099","session":"s_abc"}
```

### 5.9 Connection close

When the socket is closed from the client side without explicit `close`
messages:
1. Subprocesses for this connection are SIGTERM'd (500 ms grace → SIGKILL).
2. Sessions are **detached**, not deleted. They are reapable after
   `IDLE_TIMEOUT`.
3. A new connection may reattach by opening the same `session` with
   `resume: true`.

### 5.10 Errors

Errors are `ccsockd.error` frames with a machine-readable `code`. The daemon
never crashes the process on a per-session error.

```json
{"type":"ccsockd.error","id":"req_001","session":"s_abc","code":"claude_crashed","message":"stderr tail: ..."}
```

Error codes the client must handle:

| Code | Meaning | Fatal to connection? |
|---|---|---|
| `protocol_mismatch` | Incompatible protocol version. | Yes. |
| `invalid_message` | Malformed JSON or bad field. | No. |
| `unknown_message` | Unknown `ccsockd.*` type. | No. |
| `unsafe_flag` | Client requested a refused flag. | No. |
| `session_unknown` | No such session. | No. |
| `session_exists` | Session id collides on open. | No. |
| `session_busy` | Another turn in flight. | No. |
| `spawn_failed` | `claude` binary missing or launch failed. | No. |
| `claude_crashed` | Subprocess exited unexpectedly mid-turn. | No. |
| `oauth_expired` | OAuth token expired (stderr-detected). | No. |
| `oversize_message` | Inbound frame too large. | Yes. |
| `slow_consumer` | Per-connection queue stalled. | Yes. |
| `daemon_shutdown` | Daemon shutting down. | Yes. |
| `internal` | Unexpected daemon error. | No. |

---

## 6. Subprocess Management

### 6.1 Launch invocation

Construct argv dynamically from the open message's fields (§5.4). Always
included:

```
claude -p
  --verbose
  --session-id <s>    OR    --resume <s>
  --input-format  <v>   # default stream-json
  --output-format <v>   # default stream-json
  [all other flags per §5.4 mapping, only when set]
```

Spawn context:
- `cwd` = `open.cwd` or daemon cwd. Do a real chdir in the child (use
  `asyncio.create_subprocess_exec(cwd=...)`).
- Inherit daemon env (carries `ANTHROPIC_TOKEN` /
  `CLAUDE_CODE_OAUTH_TOKEN` / `~/.claude/.credentials.json` access).
- stdin/stdout/stderr = `asyncio.subprocess.PIPE`.

### 6.2 stdin — feeding user messages

Each client `ccsockd.user` becomes one line on the subprocess stdin, in
Claude Code's stream-json input shape. Canonical form for simple text:
```json
{"type":"user","message":{"role":"user","content":"<text>"},"session_id":"<session>"}
```
For `content` arrays, the daemon passes them through:
```json
{"type":"user","message":{"role":"user","content":[...content blocks...]},"session_id":"<session>"}
```
Flush after each line.

Writes to stdin must be queued: only one turn in flight at a time per
session. If the client sends another `ccsockd.user` while the subprocess has
not yet emitted a `result` event, the daemon replies with
`error{code:"session_busy"}` and drops the message.

### 6.3 stdout — event pass-through

The daemon reads stdout line-by-line, parses each line as JSON, injects
`"session":"<id>"` at the top level, and forwards as one JSON line to the
client. Non-JSON stdout is logged and dropped (should not occur; indicates a
CC bug).

The daemon tracks `result` events to know when the turn has ended and the
session is ready for the next message.

### 6.4 Interrupt mechanism

Per §5.7. Implementation notes:
- Use `subprocess.send_signal(SIGTERM)`. On macOS and Linux, that's
  equivalent to `proc.terminate()`.
- After 500 ms, if `proc.returncode is None`, `proc.kill()`.
- Await `proc.wait()` before respawn.
- Respawn uses the **same** stored launch argv from the original open, but
  with `--session-id X` replaced by `--resume X`.

### 6.5 Session file management

Claude Code stores session state at
`~/.claude/projects/<cwd-hash>/<session-id>.jsonl`. The daemon does not parse
these files. On `close` with `delete: true`, it removes the specific file.

Optional startup housekeeping: remove session files older than
`SESSION_RETENTION_DAYS` (default 7). Opt-in via config.

### 6.6 Warm pool (deferred)

Not in v0.1. Future optimization: maintain a pre-spawned `claude -p` per
common model for instant `open`. Cost: one idle subprocess per hot model.

---

## 7. Security

- **Socket path:** `$XDG_RUNTIME_DIR/ccsockd.sock` on Linux. On macOS, which
  lacks `$XDG_RUNTIME_DIR`, use `/tmp/ccsockd-$UID.sock`. Configurable via
  `--socket`.
- **Permissions:** socket created with mode `0600`. If the path exists on
  startup and is not owned by the current UID, refuse to start.
- **No authentication beyond socket perms.** Anyone who can `connect()` the
  socket gets full access to the user's Claude subscription.
- **No remote access.** No TCP listener. For remote use, forward via SSH.
- **Peer identity:** the daemon captures `SO_PEERCRED` (Linux) /
  `LOCAL_PEERCRED` (macOS) at connect time and logs peer PID/UID.
  Informational only; no enforcement in v0.1.
- **Secret handling:** `system_prompt`, `ccsockd.user` content, and event
  deltas are never logged at INFO+. At DEBUG, bodies are redacted to
  `<redacted N chars>`. OAuth tokens are never logged.

---

## 8. Configuration

Config file (optional): `~/.config/ccsockd/config.toml`. CLI flags and env
vars override. Env prefix: `CCSOCKD_`.

| Key | CLI flag | Env var | Default |
|---|---|---|---|
| `socket_path` | `--socket` | `CCSOCKD_SOCKET` | `$XDG_RUNTIME_DIR/ccsockd.sock` |
| `claude_bin` | `--claude` | `CCSOCKD_CLAUDE` | `claude` on PATH |
| `log_level` | `--log-level` | `CCSOCKD_LOG_LEVEL` | `info` |
| `log_file` | `--log-file` | `CCSOCKD_LOG_FILE` | stderr |
| `max_line_bytes` | — | `CCSOCKD_MAX_LINE` | `16777216` |
| `idle_timeout_s` | — | `CCSOCKD_IDLE_TIMEOUT` | `900` |
| `session_retention_days` | — | — | `7` (0 disables) |
| `max_sessions_per_connection` | — | — | `32` |
| `max_concurrent_sessions` | — | — | `64` |
| `stderr_rate_lines` | — | — | `50` |
| `stderr_rate_window_s` | — | — | `10` |

CLI:
```
ccsockd [--socket PATH] [--claude PATH] [--log-level LEVEL] [--log-file PATH]
        [--config FILE] [--version]
```

v0.1 runs in the foreground only. Use systemd/launchd for background.

### 8.1 systemd user unit (ship in `packaging/ccsockd/ccsockd.service`)

```ini
[Unit]
Description=Headless Claude Code daemon
After=default.target

[Service]
ExecStart=%h/.local/bin/ccsockd
Restart=on-failure
RestartSec=2s

[Install]
WantedBy=default.target
```

### 8.2 launchd plist (ship in `packaging/ccsockd/com.ccsock.ccsockd.plist`)

Standard KeepAlive-on-crash plist with `ThrottleInterval=5`.

---

## 9. Error Handling and Recovery

### 9.1 Subprocess crash mid-turn
On EOF on stdout or non-zero exit during a turn:
```json
{"type":"ccsockd.error","session":"s_abc","code":"claude_crashed","message":"<stderr tail>"}
```
Session remains open. Next `ccsockd.user` respawns via `--resume`.

### 9.2 OAuth expired
Detect patterns in stderr: `401`, `OAuth token expired`, `Please run claude auth`, `Session authentication failed`. Emit:
```json
{"type":"ccsockd.error","session":"s_abc","code":"oauth_expired","message":"Run `claude auth` to re-authenticate."}
```
Do not retry automatically. Subsequent user messages repeat the error until
the user re-auths and the daemon sees a successful spawn.

### 9.3 Backpressure
Bounded per-connection event queue (default 1024). When full, pause reading
from the subprocess until the queue drains. If blocked > 30 s, emit
`error{code:"slow_consumer"}` and close the connection. Sessions stay alive,
detached, subject to idle timeout.

### 9.4 Malformed client message
Reply `error{code:"invalid_message"}`, continue connection. Do not kill
sessions.

### 9.5 Daemon shutdown (SIGINT/SIGTERM)
1. Stop accepting new connections.
2. Emit `error{code:"daemon_shutdown"}` on every live connection.
3. SIGTERM every child. 2 s later, SIGKILL stragglers.
4. Close sockets, unlink socket file.
5. Exit 0.

Overall shutdown budget 5 s, then force-exit 1.

### 9.6 Stale socket file on startup
- `connect()` succeeds → another daemon is running; exit 1 with message.
- `connect()` fails → stale; unlink and continue.

---

## 10. Logging

- Structured JSON logs, one object per line, to stderr by default.
- Every line has `ts`, `level`, `event`, plus `connection_id` /
  `session_id` where applicable.
- Never log `system_prompt`, `user.text`, `user.content`, event deltas, or
  stderr subprocess output bodies at INFO+. At DEBUG, redact to
  `<redacted N chars>`.
- INFO events to include:
  - daemon start/stop (socket path, claude version, pid)
  - connection open/close (peer pid, uid)
  - session open/close (model, resume flag — NOT prompts)
  - subprocess spawn/exit (pid, exit code)
  - error frames emitted
  - interrupt received

---

## 11. Testing Requirements

### 11.1 Unit (no `claude` required)
- `test_protocol.py`: encode/decode every message type; malformed inputs;
  oversize frames; UTF-8 edge cases (surrogate pairs, NUL bytes).
- `test_session.py`: session table lifecycle; idle-timeout reaper;
  reattach by session id; delete-on-close.

### 11.2 Mock-claude tests
Provide a Python stub `claude` script that reads stream-json on stdin and
emits scripted stream-json events on stdout. Tests:
- Full turn → `result` event → `ccsockd.user` works again.
- Crash mid-turn → `claude_crashed`, next turn respawns.
- Interrupt → SIGTERM observed, respawn with `--resume`, continues.
- Concurrent sessions (3 parallel) do not interfere.
- `--session-id` vs `--resume` flag mapping is correct.
- Unsafe flags (e.g. `--dangerously-skip-permissions`) are rejected at the
  `ccsockd.open` stage.

### 11.3 End-to-end tests (`requires_claude` pytest mark)
Skipped unless the real `claude` CLI is installed and authenticated.
- Turn → text response, `result` event seen.
- Context preserved across two turns in one connection.
- Close → reattach from new connection with `resume: true` → context intact.
- Interrupt mid-generation → respawn → continuation works.

### 11.4 Latency benchmarks (`python -m ccsock.bench`)
Acceptance targets on an ordinary dev machine:
- Cold open → first event ≤ 1.5 s.
- Warm user → first event ≤ 0.5 s.
- Resume open → first event ≤ 1.5 s.

These mirror the Hermes spike numbers (0.98 s cold, ~0.8 s resume).

---

## 12. Versioning

- Protocol: `ccsock/1` in v0.1. Breaking changes bump to `ccsock/2`. Daemons
  MAY support multiple protocol versions; clients MUST request one.
- Daemon: semver. `0.x` unstable; breaking changes allowed pre-1.0.

---

## 13. Out of Scope (v0.1)

- Remote TCP/TLS access (use SSH forwarding).
- Windows support.
- Metrics endpoint (prometheus etc.).
- GUI/admin interface.
- Warm subprocess pool (future optimization).
- Passing/rewriting the OAuth token. Clients inherit whatever the daemon
  process has.
- Automatic `claude` binary updates.

---

## 14. Deliverables Checklist

- [ ] `ccsock/` package per §4.
- [ ] `ccsockd` console script in `pyproject.toml`.
- [ ] Full wire protocol per §5.
- [ ] Subprocess manager per §6.
- [ ] Unit + mock tests per §11.1–11.2.
- [ ] E2E tests per §11.3, gated by `requires_claude`.
- [ ] systemd unit + launchd plist in `packaging/ccsockd/`.
- [ ] `ccsock/README.md`: install, protocol summary, worked client example
      (Appendix A).
- [ ] Reference client `ccsock/client.py` (≤200 lines, stdlib only) with:
      `async with CcsockClient.connect() as c`,
      `async with c.open_session(**kwargs) as sess`,
      `await sess.send_user(text | content=[...])`,
      `async for event in sess.events()`,
      `await sess.interrupt()`.

---

## Appendix A: Reference client example

```python
import asyncio, uuid
from ccsock.client import CcsockClient

async def main():
    async with CcsockClient.connect() as c:
        async with c.open_session(
            session=str(uuid.uuid4()),
            model="sonnet",
            system_prompt="You are a terse assistant. Answer in one sentence.",
            tools="",                       # client wants pure inference
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

## Appendix B: Reserved message types (refuse with `unknown_message` in v0.1)

Do not treat as unknown; refuse explicitly so the wire versioning stays clean:

- `ccsockd.ping` / `ccsockd.pong` — liveness (v0.2).
- `ccsockd.status` — daemon introspection (v0.2).
- `ccsockd.list_sessions` — enumerate (v0.2).
- `ccsockd.watch` — tail events from a session without driving it (v0.2).
