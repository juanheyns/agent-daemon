# blemeesd — Headless agent daemon

**Version:** 0.1
**Language:** Python 3.11+, stdlib only (no runtime deps). Type-hinted.
**Target OS:** Linux, macOS. Windows not supported.

This document is both the README and the authoritative protocol spec.
Machine-readable JSON Schemas live under [`schemas/`](schemas/).

---

## 0. Install

Python 3.11+. No runtime dependencies outside the standard library.
The `claude` binary must be on `$PATH` (or pass `--claude`).

PyPI is the canonical source — every channel below pulls the same wheel
from there.

```bash
# pip (any environment):
pip install blemees

# uv (isolated CLI tool, fast):
uv tool install blemees

# pipx (isolated CLI tool, classic):
pipx install blemees

# Homebrew (macOS / Linux):
brew tap blemees/tap
brew install blemees
```

From source for development:

```bash
git clone https://github.com/blemees/blemees-daemon
cd blemees-daemon
uv pip install -e ".[dev]"      # or: pip install -e ".[dev]"
```

Run in the foreground:

```bash
blemeesd                          # socket at $XDG_RUNTIME_DIR/blemeesd.sock
blemeesd --socket /tmp/blemeesd.sock
blemeesd --log-level debug
```

Socket permissions are `0600`. Anyone who can `connect()` the socket has
full access to your Claude subscription, so guard it like an SSH agent.

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

### `brew services` (after `brew install`)

```bash
brew services start blemees
```

The Homebrew formula ships a service stanza so the daemon runs at login
without you touching launchd by hand.

---

## 1. Overview

`blemeesd` is a per-user daemon that exposes the Claude Code CLI (`claude -p`)
as a long-running, multi-session backend over a Unix domain socket. It is a
thin, general-purpose wrapper: clients get a headless agent they can
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
- Multi-user daemons. One `blemeesd` per OS user. Socket perms (0600) are the
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
│ blemeesd (single asyncio event loop)                  │
│                                                      │
│   UnixServer  listens on $XDG_RUNTIME_DIR/blemeesd.sock
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
blemees/
  __init__.py
  __main__.py       # python -m blemees → daemon entry point
  daemon.py         # UnixServer + connection dispatcher
  protocol.py       # wire protocol codec, message dataclasses
  session.py        # SessionTable
  subprocess.py     # ClaudeSubprocess wrapper (spawn, stream, kill, resume)
  config.py         # config loading (file + env + CLI)
  errors.py         # typed exceptions
  logging.py        # structured logging helpers
  client.py         # reference Python client (~200 lines, stdlib only)
tests/blemees/
  test_protocol.py
  test_session.py
  test_subprocess.py
  test_daemon_mock.py  # mock `claude` stub
  test_daemon_e2e.py   # requires real `claude`, gated
```

Package is self-contained (no external imports outside stdlib). A console
script `blemeesd` in `pyproject.toml` maps to `python -m blemees`.

---

## 5. Wire Protocol

Machine-readable JSON Schemas for every frame in this section live
under `schemas/` (Draft 2020-12). See `schemas/README.md` for layout
and usage. This prose is the human-facing spec; the schemas are the
contract.

### 5.1 Framing

- Transport: `AF_UNIX` stream socket.
- Framing: UTF-8 newline-delimited JSON. Exactly one JSON object per line.
- Max line size: 16 MiB (configurable). Oversize → connection closed with an
  `error` frame.
- Full duplex. Neither side should block on write (see §9.3).

#### Client socket resolution

Clients using `BlemeesClient.connect()` (and the daemon itself for its own
default) resolve the socket path in this order of precedence, stopping at
the first match:

1. `$BLEMEESD_SOCKET` — explicit override, wins everywhere.
2. `$XDG_RUNTIME_DIR/blemeesd.sock` — typical on Linux user sessions.
3. `/tmp/blemeesd-<uid>.sock` — macOS and Linux without XDG.

Only set `BLEMEESD_SOCKET` in the client's environment when the daemon
was started with a non-default path (e.g. via `blemeesd --socket …`).

### 5.2 Message namespacing

Every `type` on the wire carries an explicit namespace prefix:

| Prefix | Emitted by | Purpose |
|---|---|---|
| `blemeesd.*` | client → daemon, daemon → client | Session lifecycle and daemon operations: `hello`, `hello_ack`, `open`, `opened`, `close`, `closed`, `interrupt`, `interrupted`, `error`, `stderr`, `replay_gap`, `list_sessions`, `sessions`. |
| `claude.*` | client → daemon, daemon → client | Conversation messages. Inbound (`claude.user`) is the client's user turn, which the daemon translates to `claude -p` stream-json stdin. Outbound is everything the daemon forwards from CC's stdout, namespaced by prepending `claude.` to the native `type` (e.g. `claude.system`, `claude.stream_event`, `claude.assistant`, `claude.user`, `claude.result`, `claude.partial_assistant`). Inner payloads (e.g. the `event` field of a stream event) are not rewritten. |

Rationale: two stable namespaces — one for session lifecycle, one for
the conversation stream in either direction. Clients can switch-case on
`type` without worrying about collisions, and a `claude.user` sent and a
`claude.user` echoed back live in the same namespace because they are
the same conceptual thing.

### 5.3 Handshake

Client opens the connection and sends:
```json
{"type":"blemeesd.hello","client":"your-tool/0.1","protocol":"blemees/1"}
```
Daemon replies:
```json
{"type":"blemeesd.hello_ack","daemon":"blemeesd/0.1","protocol":"blemees/1","pid":12345,"claude_version":"2.1.118"}
```
If `protocol` does not match, daemon sends `blemeesd.error` (code
`protocol_mismatch`) and closes.

### 5.4 Session open

Client supplies whichever `claude -p` flags it wants. All fields except
`session` are OPTIONAL; the daemon omits corresponding flags when unset,
letting Claude Code apply its defaults.

```json
{
  "type": "blemeesd.open",
  "id": "req_001",
  "session_id": "s_abc",

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

Fields the daemon owns and refuses to accept from clients (rejected with
`invalid_message` on open):
`input_format`, `output_format`. Both are fixed to `stream-json`; the event
multiplexer requires it, so they are not client-tunable knobs.

Daemon reply on success:
```json
{"type":"blemeesd.opened","id":"req_001","session_id":"s_abc","subprocess_pid":54321}
```
On failure:
```json
{"type":"blemeesd.error","id":"req_001","session_id":"s_abc","code":"spawn_failed","message":"..."}
```

### 5.5 User message

Client sends a new user turn to an open session. The `message` field is
passed through verbatim to `claude -p`'s stream-json stdin — the daemon
only rewrites the envelope (`claude.user` → `user`, `session` →
`session_id`).

Simple text:
```json
{"type":"claude.user","session_id":"s_abc","message":{"role":"user","content":"Hello"}}
```

Multimodal: `content` may be an array of CC stream-json blocks:
```json
{"type":"claude.user","session_id":"s_abc","message":{"role":"user","content":[{"type":"text","text":"What is in this image?"},{"type":"image","source":{"type":"base64","media_type":"image/png","data":"..."}}]}}
```

`message.role` must be `"user"`. `message.content` must be a string or an
array of CC content blocks. Any additional fields CC may add to
`message` in the future will pass through unchanged; the daemon does not
validate them.

No `id` required. Responses stream as events until the turn ends.

### 5.6 Event stream (daemon → client)

The daemon reads each line of `claude -p` stdout, parses as JSON, injects
`"session_id":"<id>"`, and prepends `claude.` to the native `type` before
forwarding. Clients see CC event shapes under a stable namespace
(`claude.system`, `claude.stream_event`, `claude.assistant`,
`claude.user`, `claude.result`, `claude.partial_assistant`, etc.). The
inner payload (e.g. the `event` field of a `stream_event`) is untouched.

Example (abridged):
```json
{"session_id":"s_abc","type":"claude.system","subtype":"init","model":"claude-sonnet-4-6","tools":["Bash","Read","Edit"]}
{"session_id":"s_abc","type":"claude.stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":"Hel"}}}
{"session_id":"s_abc","type":"claude.stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":"lo"}}}
{"session_id":"s_abc","type":"claude.assistant","message":{"role":"assistant","content":[{"type":"text","text":"Hello"}]}}
{"session_id":"s_abc","type":"claude.result","subtype":"success","duration_ms":1254,"num_turns":1}
```

The daemon does NOT translate, filter, deduplicate, or re-shape these events.
Clients that only want streaming deltas should ignore the final `assistant`
echo to avoid double-counting.

Events that arrive on the subprocess's **stderr** are wrapped and forwarded
as well (for visibility into CC warnings / auth errors):
```json
{"session_id":"s_abc","type":"blemeesd.stderr","line":"..."}
```
These are rate-limited to prevent a broken subprocess from flooding the
client. Default cap: 50 lines per 10 s; excess dropped with a counter.

### 5.7 Interrupt

Client cancels the in-flight turn:
```json
{"type":"blemeesd.interrupt","session_id":"s_abc"}
```

Daemon:
1. Sends SIGTERM to the subprocess. After 500 ms, SIGKILL if still alive.
2. Emits `blemeesd.interrupted`:
   ```json
   {"type":"blemeesd.interrupted","session_id":"s_abc"}
   ```
3. Respawns the subprocess immediately with `--resume <session>` (all other
   flags identical to the original open), so the next `claude.user` works
   without further ceremony.

Any CC events emitted before the kill are forwarded as normal. Already-sent
deltas are NOT retracted.

Interrupt is a no-op (returns `blemeesd.interrupted` with `was_idle: true`) if
no turn is in flight.

### 5.8 Close

Explicit session close:
```json
{"type":"blemeesd.close","id":"req_099","session_id":"s_abc","delete":true}
```
- `delete: true` → daemon removes the CC session file from disk after kill.
- `delete: false` (default) → session file retained for later `resume: true`.

Daemon replies:
```json
{"type":"blemeesd.closed","id":"req_099","session_id":"s_abc"}
```

### 5.9 Connection close

When the socket is closed from the client side without explicit `close`
messages (soft detach):

1. The writer attached to each session is unhooked immediately so no more
   frames are pushed to the dead socket.
2. **If a turn is in flight**, the subprocess is *not* killed — it keeps
   running to completion and the session is marked "finishing". Events
   continue to accumulate in the session's ring buffer (§5.11) and in
   the durable log if enabled, so a client that reconnects can replay
   them via `last_seen_seq`. When the subprocess next emits
   `claude.result`, the daemon gracefully terminates it.
3. **If no turn is in flight**, the subprocess is terminated immediately
   (SIGTERM → 500 ms → SIGKILL).
4. Either way, the session record is *detached*, not deleted:
   `connection_id = None`, `detached_at = now()`. It is reapable after
   `IDLE_TIMEOUT` (during which a late-finishing turn will be torn down
   along with the session).
5. A new connection may reattach by opening the same `session` with
   `resume: true`, optionally passing `last_seen_seq` to catch up on
   anything it missed while disconnected.

Rationale: a hard kill mid-turn left Claude Code's on-disk transcript in
whatever partially-flushed state the SIGTERM grace allowed, silently
diverging the model's conversation state from what the client last saw.
Letting the turn complete closes the transcript cleanly and makes mid-
stream reconnects a replay problem, not a consistency problem.

#### 5.9.1 Session takeover

A second connection may open a session that is currently owned by
another live connection (via `resume: true`). The daemon allows the
takeover and notifies the previous owner *before* switching the writer:

```json
{"type":"blemeesd.session_taken","session_id":"s_abc","by_peer_pid":12345}
```

After this frame the previous connection stops receiving events for
that session; its other sessions (if any) are unaffected and its
socket stays open. `by_peer_pid` reflects the new owner's peer PID
from `SO_PEERCRED` when available, for debugging/audit; it is absent
when the kernel or platform does not expose it.

If the ex-owner wants the session back, it may itself send `open`
with `resume: true` — which will in turn notify the current owner.
Ping-pong is the clients' problem; the daemon does not arbitrate.

The new owner's subsequent replay (via `last_seen_seq`) works as
usual — the ring buffer is session-local, not connection-local, so
frames emitted while the ex-owner held the writer are still
available to the new owner.

### 5.10 Errors

Errors are `blemeesd.error` frames with a machine-readable `code`. The daemon
never crashes the process on a per-session error.

```json
{"type":"blemeesd.error","id":"req_001","session_id":"s_abc","code":"claude_crashed","message":"stderr tail: ..."}
```

Error codes the client must handle:

| Code | Meaning | Fatal to connection? |
|---|---|---|
| `protocol_mismatch` | Incompatible protocol version. | Yes. |
| `invalid_message` | Malformed JSON or bad field. | No. |
| `unknown_message` | Unknown `blemeesd.*` type. | No. |
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

### 5.11 Event stream durability (seq, ring buffer, replay)

Every outbound frame the daemon emits for a session — both forwarded
`claude.*` events and synthetic `blemeesd.*` frames — carries a
monotonic integer `seq`, assigned by the session and starting at 1.
`blemeesd.opened` additionally carries `last_seq` so a reconnecting
client knows the highest seq the session has produced.

Recent frames are retained in two places:

* **In-memory ring buffer**, per session, bounded (default 1024;
  `BLEMEESD_RING_BUFFER_SIZE`). Always on. Survives client disconnects
  but not daemon restarts.
* **Durable event log**, per session, opt-in
  (`BLEMEESD_EVENT_LOG_DIR`). Append-only JSONL at
  `<dir>/<session>.jsonl`. On session reopen the ring is seeded from
  the log's tail, so replay survives daemon restarts. `close
  {delete:true}` unlinks the log.

On reconnect, the client may request replay:

```json
{"type":"blemeesd.open","id":"r1","session_id":"s1","resume":true,"last_seen_seq":42}
```

The daemon delivers, in order:
1. `blemeesd.opened` (with `last_seq`), then
2. every buffered frame with `seq > last_seen_seq`, then
3. live frames.

If the buffer has rolled over past `last_seen_seq + 1`, a one-shot
`blemeesd.replay_gap{since_seq, first_available_seq}` frame is emitted
before the replay so the client can detect the loss:

```json
{"type":"blemeesd.replay_gap","session_id":"s1","since_seq":42,"first_available_seq":71}
```

Omitting `last_seen_seq` on reattach replays whatever is currently in
the ring. Passing `last_seen_seq` equal to the session's current `seq`
skips replay and goes straight to live delivery.

### 5.12 Liveness (ping / pong)

Client:
```json
{"type":"blemeesd.ping","id":"req_1","data":"anything"}
```
Daemon:
```json
{"type":"blemeesd.pong","id":"req_1","data":"anything"}
```
`data` is opaque and echoed verbatim. `id` is recommended for
round-trip correlation. Both fields are optional.

### 5.13 Status introspection

Client:
```json
{"type":"blemeesd.status","id":"req_2"}
```
Daemon:
```json
{
  "type":"blemeesd.status_reply","id":"req_2",
  "daemon":"blemeesd/0.1.0","protocol":"blemees/1","pid":12345,
  "claude_version":"2.1.118","uptime_s":127.3,
  "socket_path":"/run/user/1000/blemeesd.sock",
  "connections":3,
  "sessions":{"total":5,"attached":4,"detached":1,"active_turns":2},
  "config":{
    "ring_buffer_size":1024,"event_log_enabled":false,
    "idle_timeout_s":900,"shutdown_grace_s":30,
    "max_concurrent_sessions":64,"max_line_bytes":16777216
  }
}
```
No side effects. Forward-compatible: new fields may be added inside
`sessions` / `config`, and new top-level keys may appear.

### 5.14 Watch (subscribe-only observer)

A second connection may subscribe to an existing session's event
stream without taking ownership. The owner keeps driving the session;
watchers receive the same `claude.*` events, `blemeesd.stderr`,
`blemeesd.error{claude_crashed,oauth_expired}`, and `blemeesd.replay_gap`
frames the owner does, plus an optional replay on subscribe.

Client:
```json
{"type":"blemeesd.watch","id":"req_3","session_id":"s_abc","last_seen_seq":0}
```
Daemon (ack, then event stream):
```json
{"type":"blemeesd.watching","id":"req_3","session_id":"s_abc","last_seq":42}
```
Unknown session → `blemeesd.error{code:"session_unknown"}`. Multiple
connections may watch the same session. Watchers cannot drive:
`claude.user`, `blemeesd.interrupt`, `blemeesd.close`, and
`blemeesd.session_taken` remain connection-scoped to the owner.

Unsubscribe:
```json
{"type":"blemeesd.unwatch","id":"req_4","session_id":"s_abc"}
```
Reply:
```json
{"type":"blemeesd.unwatched","id":"req_4","session_id":"s_abc","was_watching":true}
```
Watchers are also automatically removed when the connection closes.

### 5.15 Session info (usage + turn counters)

Query a session's cumulative token usage, turn count, and last-turn
snapshot. Side-effect-free.

Client:
```json
{"type":"blemeesd.session_info","id":"req_5","session_id":"s_abc"}
```
Daemon:
```json
{
  "type":"blemeesd.session_info_reply","id":"req_5","session_id":"s_abc",
  "model":"claude-sonnet-4-6","cwd":"/home/u/proj",
  "turns":5,
  "last_turn_at_ms":1745000000000,
  "last_turn_usage":{
    "input_tokens":500,"output_tokens":200,
    "cache_read_input_tokens":14000,"cache_creation_input_tokens":0
  },
  "cumulative_usage":{
    "input_tokens":3000,"output_tokens":1200,
    "cache_read_input_tokens":70000,"cache_creation_input_tokens":100
  },
  "context_tokens":14500,
  "attached":true,"subprocess_running":true,
  "last_seq":42
}
```

The accumulator is maintained from each `claude.result` event's
`usage` block (fields pass through verbatim; future Anthropic-added
keys appear automatically). `context_tokens` is the sum of the last
turn's input-side tokens (fresh + `cache_read` + `cache_creation`)
— compare to the model's context window to gauge headroom.

**Persistence**: when `event_log_dir` is enabled, the counters are
written to `<event_log_dir>/<session>.usage.json` on every turn
(atomic rename) and reloaded on session reopen, so they survive
daemon restarts. Without the durable log they are in-memory only and
reset to zero on restart. `blemeesd.close {delete:true}` also
unlinks the sidecar.

---

## 6. Subprocess Management

### 6.1 Launch invocation

Construct argv dynamically from the open message's fields (§5.4). Always
included:

```
claude -p
  --verbose
  --session-id <s>    OR    --resume <s>
  --input-format  stream-json   # fixed by the daemon; not client-settable
  --output-format stream-json   # fixed by the daemon; not client-settable
  [all other flags per §5.4 mapping, only when set]
```

Spawn context:
- `cwd` = `open.cwd` or daemon cwd. Do a real chdir in the child (use
  `asyncio.create_subprocess_exec(cwd=...)`).
- Inherit daemon env (carries `ANTHROPIC_TOKEN` /
  `CLAUDE_CODE_OAUTH_TOKEN` / `~/.claude/.credentials.json` access).
- stdin/stdout/stderr = `asyncio.subprocess.PIPE`.

### 6.2 stdin — feeding user messages

Each client `claude.user` becomes one line on the subprocess stdin, in
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
session. If the client sends another `claude.user` while the subprocess has
not yet emitted a `result` event, the daemon replies with
`error{code:"session_busy"}` and drops the message.

### 6.3 stdout — event pass-through

The daemon reads stdout line-by-line, parses each line as JSON, injects
`"session_id":"<id>"` at the top level, and forwards as one JSON line to the
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

---

## 7. Security

- **Socket path:** `$XDG_RUNTIME_DIR/blemeesd.sock` on Linux. On macOS, which
  lacks `$XDG_RUNTIME_DIR`, use `/tmp/blemeesd-$UID.sock`. Configurable via
  `--socket`.
- **Permissions:** socket created with mode `0600`. If the path exists on
  startup and is not owned by the current UID, refuse to start.
- **No authentication beyond socket perms.** Anyone who can `connect()` the
  socket gets full access to the user's Claude subscription.
- **No remote access.** No TCP listener. For remote use, forward via SSH.
- **Peer identity:** the daemon captures `SO_PEERCRED` (Linux) /
  `LOCAL_PEERCRED` (macOS) at connect time and logs peer PID/UID.
  Informational only; no enforcement in v0.1.
- **Secret handling:** `system_prompt`, `claude.user` content, and event
  deltas are never logged at INFO+. At DEBUG, bodies are redacted to
  `<redacted N chars>`. OAuth tokens are never logged.

---

## 8. Configuration

Config file (optional): `~/.config/blemeesd/config.toml`. CLI flags and env
vars override. Env prefix: `BLEMEESD_`.

| Key | CLI flag | Env var | Default |
|---|---|---|---|
| `socket_path` | `--socket` | `BLEMEESD_SOCKET` | `$XDG_RUNTIME_DIR/blemeesd.sock` |
| `claude_bin` | `--claude` | `BLEMEESD_CLAUDE` | `claude` on PATH |
| `log_level` | `--log-level` | `BLEMEESD_LOG_LEVEL` | `info` |
| `log_file` | `--log-file` | `BLEMEESD_LOG_FILE` | stderr |
| `max_line_bytes` | — | `BLEMEESD_MAX_LINE` | `16777216` |
| `idle_timeout_s` | — | `BLEMEESD_IDLE_TIMEOUT` | `900` |
| `session_retention_days` | — | — | `7` (0 disables) |
| `max_sessions_per_connection` | — | — | `32` |
| `max_concurrent_sessions` | — | — | `64` |
| `stderr_rate_lines` | — | — | `50` |
| `stderr_rate_window_s` | — | — | `10` |

CLI:
```
blemeesd [--socket PATH] [--claude PATH] [--log-level LEVEL] [--log-file PATH]
        [--config FILE] [--version]
```

v0.1 runs in the foreground only. Use systemd/launchd for background.

### 8.1 systemd user unit (ship in `packaging/blemeesd/blemeesd.service`)

```ini
[Unit]
Description=Headless agent daemon
After=default.target

[Service]
ExecStart=%h/.local/bin/blemeesd
Restart=on-failure
RestartSec=2s

[Install]
WantedBy=default.target
```

### 8.2 launchd plist (ship in `packaging/blemeesd/com.blemees.blemeesd.plist`)

Standard KeepAlive-on-crash plist with `ThrottleInterval=5`.

### 8.3 Service lifecycle

`blemeesd` is a **per-user** daemon by design — one instance per UID, one
Claude account per instance, socket pinned to that UID. Every install path
above registers it with a per-user service manager, not a system one.

**macOS — LaunchAgent.** `brew services start blemees` writes
`~/Library/LaunchAgents/homebrew.mxcl.blemees.plist` and loads it into your
GUI session via `launchctl`. Manual install writes
`~/Library/LaunchAgents/com.blemees.blemeesd.plist`. Either way it:

- starts at login, restarts on crash (`KeepAlive`),
- stops at logout (a power cycle with no login leaves it off),
- runs as you, so `~/.claude/` creds and session logs are yours.

Socket: `/tmp/blemeesd-<uid>.sock`.
Inspect: `brew services list`, `launchctl list | grep blemees`,
`tail -f "$(brew --prefix)/var/log/blemees/blemeesd.err.log"`.

**Linux — systemd `--user` unit.** `brew services start blemees` writes
`~/.config/systemd/user/homebrew.blemees.service`. Manual install writes
`~/.config/systemd/user/blemeesd.service`. Either way it:

- starts when your user manager starts (first login after boot),
- stops when your last session ends (SSH out, logout),
- runs as you.

Socket: `$XDG_RUNTIME_DIR/blemeesd.sock` (= `/run/user/<uid>/blemeesd.sock`).
Inspect: `systemctl --user status blemeesd`, `journalctl --user -u blemeesd -f`.

#### Finding the `claude` binary

Services do **not** inherit your shell's `PATH`. `brew services` and
systemd `--user` start with a minimal `PATH` (`/usr/bin:/bin:...`) plus
whatever the unit file adds. The tap formula extends it to cover
`~/.local/bin`, `~/bin`, and `$HOMEBREW_PREFIX/bin`, which is where the
standalone installer puts `claude`. The symptom when this is wrong is a
healthy `daemon.start` line but every session ending in `spawn_failed`.

If your `claude` lives elsewhere (npm global under `~/.nvm/...`, a
custom path, etc.), override with `BLEMEESD_CLAUDE`:

- macOS:
  ```bash
  launchctl setenv BLEMEESD_CLAUDE "$(which claude)"
  brew services restart blemees
  ```
  `launchctl setenv` persists until reboot; for durable override, add an
  `EnvironmentVariables` block to the plist.
- Linux:
  ```bash
  systemctl --user edit blemeesd
  # add in the editor:
  #   [Service]
  #   Environment="BLEMEESD_CLAUDE=/full/path/to/claude"
  systemctl --user restart blemeesd
  ```

Or bake `--claude /full/path/to/claude` into the unit's `ExecStart` /
plist `ProgramArguments`.

#### Running at boot

You probably do not want this — `claude` runs with whatever privileges the
daemon has, and a broader trust boundary means a bigger blast radius. If
you need it anyway (e.g. headless server, unattended box), these are the
supported paths. Both keep the daemon running as **one named user**; do
not run it as root.

**Linux — `loginctl enable-linger`.** Single flag, no code or unit changes:

```bash
sudo loginctl enable-linger "$USER"
```

systemd starts your user manager at boot and keeps your `--user` units
alive regardless of login state. Undo with `disable-linger`.

**macOS — hand-rolled LaunchDaemon with `UserName`.** There is no
`enable-linger` equivalent. `sudo brew services start blemees` *does*
produce a LaunchDaemon, but it runs as **root** — do not use it.
Instead, stop the user-scope service and install a LaunchDaemon that
drops to your user at launch:

```bash
brew services stop blemees
```

Write `/Library/LaunchDaemons/com.blemees.blemeesd.plist` (owned
`root:wheel`, mode `0644`):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>            <string>com.blemees.blemeesd</string>
  <key>UserName</key>         <string>YOUR_USERNAME</string>
  <key>ProgramArguments</key>
  <array>
    <string>/opt/homebrew/bin/blemeesd</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key> <string>/Users/YOUR_USERNAME</string>
    <key>PATH</key> <string>/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>RunAtLoad</key>        <true/>
  <key>KeepAlive</key>        <true/>
  <key>StandardOutPath</key>  <string>/Users/YOUR_USERNAME/Library/Logs/blemees/blemeesd.out.log</string>
  <key>StandardErrorPath</key><string>/Users/YOUR_USERNAME/Library/Logs/blemees/blemeesd.err.log</string>
</dict>
</plist>
```

Load and unload:

```bash
sudo launchctl bootstrap system /Library/LaunchDaemons/com.blemees.blemeesd.plist
sudo launchctl bootout   system /Library/LaunchDaemons/com.blemees.blemeesd.plist
```

Gotchas:

- `HOME` **must** be set in `EnvironmentVariables`; LaunchDaemons start
  with an empty env and `~/.claude/` lookups will fail otherwise.
- On FileVault-encrypted disks, "at boot" actually means "at first
  unlock of the disk at boot" — not truly pre-login.
- Intel Macs: use `/usr/local/bin` instead of `/opt/homebrew/bin`.

---

## 9. Error Handling and Recovery

### 9.1 Subprocess crash mid-turn
On EOF on stdout or non-zero exit during a turn:
```json
{"type":"blemeesd.error","session_id":"s_abc","code":"claude_crashed","message":"<stderr tail>"}
```
Session remains open. Next `claude.user` respawns via `--resume`.

### 9.2 OAuth expired
Detect patterns in stderr: `401`, `OAuth token expired`, `Please run claude auth`, `Session authentication failed`. Emit:
```json
{"type":"blemeesd.error","session_id":"s_abc","code":"oauth_expired","message":"Run `claude auth` to re-authenticate."}
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

Shutdown applies the same soft-detach policy as a client disconnect
(§5.9): sessions with an in-flight turn are allowed to run to the next
`claude.result` before being terminated, so their transcripts close
cleanly.

1. Stop accepting new connections.
2. Emit `error{code:"daemon_shutdown"}` on every live connection.
3. For every session with `turn_active=True`, set `_finishing=True`.
   Events continue to accumulate in the ring buffer and (if enabled)
   durable log, so a client that reconnects to a restarted daemon can
   replay them via `last_seen_seq`.
4. Wait up to `shutdown_grace_s` seconds (default 30) for finishing
   subprocesses to reach their next `claude.result` and self-terminate.
   Idle sessions (no turn in flight) are not subject to this wait.
5. Force phase: SIGTERM every remaining child, 500 ms grace, then
   SIGKILL stragglers. Bounded by a 5 s budget.
6. Close sockets, unlink socket file.
7. Exit 0.

Overall wall-clock budget is therefore `shutdown_grace_s + 5 s`. Past
that, the daemon force-exits 1.

Set `shutdown_grace_s=0` (via `BLEMEESD_SHUTDOWN_GRACE` env or config)
to disable the graceful phase and hard-kill immediately.

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
- Full turn → `result` event → `claude.user` works again.
- Crash mid-turn → `claude_crashed`, next turn respawns.
- Interrupt → SIGTERM observed, respawn with `--resume`, continues.
- Concurrent sessions (3 parallel) do not interfere.
- `--session-id` vs `--resume` flag mapping is correct.
- Unsafe flags (e.g. `--dangerously-skip-permissions`) are rejected at the
  `blemeesd.open` stage.

### 11.3 End-to-end tests (`requires_claude` pytest mark)
Skipped unless the real `claude` CLI is installed and authenticated.
- Turn → text response, `result` event seen.
- Context preserved across two turns in one connection.
- Close → reattach from new connection with `resume: true` → context intact.
- Interrupt mid-generation → respawn → continuation works.

### 11.4 Latency benchmarks (`python -m blemees.bench`)
Acceptance targets on an ordinary dev machine:
- Cold open → first event ≤ 1.5 s.
- Warm user → first event ≤ 0.5 s.
- Resume open → first event ≤ 1.5 s.

---

## 12. Versioning

- Protocol: `blemees/1` in v0.1. Breaking changes bump to `blemees/2`. Daemons
  MAY support multiple protocol versions; clients MUST request one.
- Daemon: semver. `0.x` unstable; breaking changes allowed pre-1.0.

---

## Appendix A: Reference client example

```python
import asyncio, uuid
from blemees.client import BlemeesClient

async def main():
    async with BlemeesClient.connect() as c:
        async with c.open_session(
            session_id=str(uuid.uuid4()),
            model="sonnet",
            system_prompt="You are a terse assistant. Answer in one sentence.",
            tools="",                       # client wants pure inference
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
