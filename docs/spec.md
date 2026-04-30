---
title: Protocol spec
nav_order: 2
permalink: /spec/
---

<!-- Auto-synced from README.md by .github/workflows/docs-sync.yml. Edit the root README. -->

# blemeesd — Headless agent daemon

**Version:** 0.1
**Protocol:** `blemees/2`
**Language:** Python 3.11+, stdlib only (no runtime deps). Type-hinted.
**Target OS:** Linux, macOS. Windows not supported.

This document is both the README and the authoritative protocol spec.
Machine-readable JSON Schemas live under [`blemees/schemas/`](blemees/schemas/) and ship inside the wheel — clients can resolve them via `importlib.resources.files("blemees.schemas")`. The unified event vocabulary is documented in [`docs/agent-events.md`](docs/agent-events.md).

---

## 0. Install

Python 3.11+. No runtime dependencies outside the standard library.
At least one of the supported agent backends must be on `$PATH`:

* **Claude Code** — `claude` binary (override with `--claude` / `BLEMEESD_CLAUDE`).
* **Codex** — `codex` binary, version 0.125+ for `codex mcp-server` (override with `--codex` / `BLEMEESD_CODEX`).

The daemon picks the backend per session, on `blemeesd.open`. A daemon
without `claude` on `$PATH` can still serve `backend:"codex"` sessions
and vice versa; the missing binary surfaces as a `spawn_failed` only
when a session that needs it is opened.

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

### Smoke-test the wire (`blemeesctl`)

The package also ships `blemeesctl`, an interactive REPL that maps
each command to one outbound wire frame and prints every inbound
frame. It's not a chat UI — it's how you poke the protocol,
sanity-check an install, or reproduce a bug from a known sequence of
frames.

> **Renamed in 0.9.0** — pre-0.9 wheels shipped this REPL as `blemees`.
> The `blemees` console_script is no longer registered by the daemon
> wheel; it's reserved for the chat TUI shipped by the
> [`blemees-tui`](https://github.com/blemees/blemees-tui) package. If
> muscle-memory has you typing `blemees`, retrain to `blemeesctl`.

```
$ blemeesctl
· connected: /tmp/blemeesd-501.sock
→ {"type":"blemeesd.hello","client":"blemeesctl/0.9.0","protocol":"blemees/2"}
← blemeesd.hello_ack  {"daemon":"blemeesd/0.9.0","backends":{"claude":"2.1.118","codex":"0.125.0"},…}
blemeesctl> status
← blemeesd.status_reply  {"uptime_s":12.4,"connections":1,…}
blemeesctl> open new backend=claude options.model=sonnet options.permission_mode=bypassPermissions
· session_id: 5a01f0d8-…
← blemeesd.opened  …
blemeesctl> send 5a01f0d8-… what is 2+2?
← agent.delta {"backend":"claude","kind":"text","text":"4"}
← agent.result {"backend":"claude","subtype":"success","duration_ms":…}
blemeesctl> close 5a01f0d8-…
```

`help` at the prompt lists every verb. Highlights: `open` / `resume` /
`close` / `interrupt` / `send` / `send-json` / `watch` / `unwatch` /
`status` / `session-info` / `sessions [cwd]` / `ping`. `raw {…}`
sends an arbitrary JSON frame for protocol experiments.

---

## 1. Overview

`blemeesd` is a per-user daemon that exposes one or more agent backends
— currently Claude Code (`claude -p`) and Codex (`codex mcp-server`) — as a
long-running, multi-session backend over a Unix domain socket. It is a
thin, general-purpose wrapper: clients get a headless agent they can
reach from any language, any process.

The daemon is **a translation layer, not a re-interpreter.** It does
not inject a system prompt, does not implement a tool protocol, does
not filter events. It:

1. Listens on a Unix socket.
2. Lets clients open, drive, interrupt, resume, and close sessions on
   either backend.
3. Translates each backend's native event stream into the unified
   `agent.*` vocabulary (see [`docs/agent-events.md`](docs/agent-events.md)).
4. Manages subprocess lifecycle (spawn, kill, respawn or re-attach
   per-backend).

Clients pick a backend per session via `blemeesd.open.backend` and pass
backend-specific knobs under `options.<backend>.*`. Any session emits
the same `agent.*` frames regardless of backend — clients can switch on
event type without branching by backend.

---

## 2. Goals and Non-Goals

### Goals (v0.1)
- Expose Claude Code and Codex over a local Unix socket, multiplexing
  multiple sessions of either backend.
- Support each backend's full non-interactive surface relevant to
  programmatic use (§6). Clients control their own system prompt, tools,
  model, cwd, etc., via `options.<backend>.*`.
- Single unified event vocabulary (`agent.*`) so clients are
  backend-agnostic on the read side.
- Session resume across client disconnects and daemon restarts.
- Interrupt: cancel the in-flight turn cleanly and allow continuation.
- Sub-second warm first-event latency; ~1 s cold start (CC).
- Be **neutral on semantics** — no client-specific assumptions, no
  built-in prompts, no tool protocols. The daemon translates event
  shapes; it does not re-interpret tool calls or model output.

### Non-goals (v0.1)
- Inventing a tool protocol. Clients either use each backend's native
  tools (CC: `options.claude.tools`, `options.claude.mcp_config`, …;
  Codex: `options.codex.config` for MCP child servers, sandboxing,
  approvals) or implement their own protocol in their own system
  prompt. The daemon does not parse assistant output.
- Cross-backend session migration (resume a Claude session on Codex,
  etc.). Each backend owns its own session storage.
- Multi-user daemons. One `blemeesd` per OS user. Socket perms (0600) are the
  only access control.
- Remote access (TCP/TLS). Use SSH socket forwarding if needed.
- Running `claude` or `codex` interactively (without programmatic stdio).
- Token refresh. If OAuth expires, surface the error and let the user
  re-authenticate against the relevant CLI manually.
- Prompt caching control, GUI/admin interface.

---

## 3. Architecture

```
┌──────────────────────────────────────────────────────────────┐
│ blemeesd (single asyncio event loop)                         │
│                                                              │
│   UnixServer  listens on $XDG_RUNTIME_DIR/blemeesd.sock      │
│      │                                                       │
│      ├─ Connection 1                                         │
│      │    ├─ Session s_abc  → ClaudeBackend (claude -p)      │
│      │    └─ Session s_def  → CodexBackend  (codex mcp-server)│
│      │                                                       │
│      └─ Connection 2                                         │
│           └─ Session s_xyz  → ClaudeBackend (claude -p)      │
│                                                              │
│   AgentBackend (per-session)                                 │
│     - spawns / drives / kills / re-attaches its child         │
│     - translates native events → agent.* frames              │
│                                                              │
│   SessionTable                                               │
│     - session_id → (connection_id?, backend, cwd)            │
│     - reaps orphans after IDLE_TIMEOUT                       │
└──────────────────────────────────────────────────────────────┘
```

- Single asyncio event loop. `asyncio.subprocess` handles stdio.
- One backend subprocess per open session. CC sessions run a
  `claude -p` child; Codex sessions run a `codex mcp-server` child.
- The backend object owns the native protocol (CC stream-json line stream
  vs. Codex JSON-RPC 2.0) and emits unified `agent.*` frames into the
  Session.
- Sessions outlive client connections (reattach via `resume: true`).
- Unattached sessions reaped after `IDLE_TIMEOUT` (default 900 s).

---

## 4. File Layout

```
blemees/
  __init__.py
  __main__.py            # python -m blemees → daemon entry point
  daemon.py              # UnixServer + connection dispatcher
  protocol.py            # wire protocol codec, message dataclasses
  session.py             # SessionTable
  backends/
    __init__.py          # AgentBackend Protocol
    claude.py            # ClaudeBackend (spawn `claude -p`, translate stream-json → agent.*)
    codex.py             # CodexBackend  (spawn `codex mcp-server`, translate JSON-RPC → agent.*)
    translate_claude.py  # CC native event → agent.* frames
    translate_codex.py   # Codex `msg.*` → agent.* frames
  config.py              # config loading (file + env + CLI)
  errors.py              # typed exceptions
  logging.py             # structured logging helpers
  client.py              # reference Python client (~200 lines, stdlib only)
tests/blemees/
  test_protocol.py
  test_session.py
  test_translate_claude.py
  test_translate_codex.py
  test_daemon_mock.py        # mock `claude` and mock `codex mcp-server` stubs
  test_daemon_e2e_claude.py  # requires real `claude`, gated by `requires_claude`
  test_daemon_e2e_codex.py   # requires real `codex`,  gated by `requires_codex`
```

Package is self-contained (no external imports outside stdlib). A console
script `blemeesd` in `pyproject.toml` maps to `python -m blemees`.

---

## 5. Wire Protocol

Machine-readable JSON Schemas for every frame in this section live
under `blemees/schemas/` (Draft 2020-12) and ship as package data in
the wheel. See `blemees/schemas/README.md` for layout and usage; the
helpers `blemees.schemas.load(name)` and `iter_schemas()` give you a
parsed schema or a stream of them without touching the filesystem.
This prose is the human-facing spec; the schemas are the contract.

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
| `blemeesd.*` | client → daemon, daemon → client | Session lifecycle and daemon operations: `hello`, `hello_ack`, `open`, `opened`, `close`, `closed`, `interrupt`, `interrupted`, `error`, `stderr`, `replay_gap`, `list_sessions`, `sessions`, `ping`, `pong`, `status`, `status_reply`, `watch`, `watching`, `unwatch`, `unwatched`, `session_taken`, `session_closed`, `session_info`, `session_info_reply`. |
| `agent.*` | client → daemon, daemon → client | Conversation messages, normalised across backends. Inbound (`agent.user`) is the client's user turn, which the daemon hands to the backend's native input mechanism (CC: stream-json stdin; Codex: `tools/call`). Outbound is the daemon's translated event stream: `agent.system_init`, `agent.delta`, `agent.message`, `agent.user_echo`, `agent.tool_use`, `agent.tool_result`, `agent.notice`, `agent.result`. Every outbound `agent.*` frame carries a `backend: "claude" \| "codex"` field; clients that want backend-native fidelity can opt into a `raw` field per session via `options.<backend>.include_raw_events: true`. The full type-by-type translation table is in [`docs/agent-events.md`](docs/agent-events.md). |

Rationale: two stable namespaces — one for session lifecycle, one for
the conversation stream in either direction. The unified `agent.*`
namespace lets clients consume Claude and Codex sessions with the same
event-handling code; backends only differ in what they accept under
`options.<backend>.*` at open time, not in what they emit on the wire.

### 5.3 Handshake

Client opens the connection and sends:
```json
{"type":"blemeesd.hello","client":"your-tool/0.1","protocol":"blemees/2"}
```
Daemon replies:
```json
{
  "type":"blemeesd.hello_ack",
  "daemon":"blemeesd/0.1",
  "protocol":"blemees/2",
  "pid":12345,
  "backends":{
    "claude":"2.1.118",
    "codex":"0.125.0"
  }
}
```
`backends` carries one entry per backend the daemon successfully
detected at startup. Backends whose binary is missing from `$PATH` are
omitted (so `{"backends":{"claude":"2.1.118"}}` is a valid ack — Codex
is just unavailable). Detection is best-effort and never blocks
startup.

If `protocol` does not match, daemon sends `blemeesd.error` (code
`protocol_mismatch`) and closes.

> **`blemees/2` note:** v2 introduces the unified `agent.*` namespace
> and the `backend` / `options` shape on `blemeesd.open`. There is no
> backwards-compatible alias for `blemees/1` — pre-1.0 clients must
> migrate. See the migration notes at the bottom of §5.

### 5.4 Session open

A session opens against a chosen backend. The frame envelope is
backend-neutral; backend-specific knobs live under `options.<backend>`.

```json
{
  "type": "blemeesd.open",
  "id": "req_001",
  "session_id": "s_abc",
  "backend": "claude",
  "resume": false,
  "last_seen_seq": 0,

  "options": {
    "claude": {
      "model": "sonnet",
      "system_prompt": "...",
      "tools": "default",
      "permission_mode": "bypassPermissions",
      "cwd": "/home/u/proj",
      "include_raw_events": false
    }
  }
}
```

Or against Codex:

```json
{
  "type": "blemeesd.open",
  "id": "req_002",
  "session_id": "s_xyz",
  "backend": "codex",
  "resume": false,

  "options": {
    "codex": {
      "model": "gpt-5.2-codex",
      "cwd": "/home/u/proj",
      "sandbox": "read-only",
      "approval-policy": "never",
      "developer-instructions": "...",
      "config": { "model_reasoning_effort": "medium" },
      "include_raw_events": false
    }
  }
}
```

`type, session_id, backend` are REQUIRED. `options` is REQUIRED but
may be `{}` (the backend will then run with its defaults). Only the
`options.<backend>` block matching the chosen backend is consulted —
extra blocks for other backends are ignored. Unknown keys *inside* an
`options.<backend>` block are rejected with `invalid_message`.

> **`session_id` must be a UUID.** The daemon treats it as opaque at
> the protocol layer (any non-empty string passes schema validation),
> but the Claude backend forwards it to `claude -p --session-id`,
> which accepts UUIDs only — non-UUIDs surface as `spawn_failed`.
> Codex uses its own `threadId`, but for backend-neutrality clients
> should always generate `str(uuid.uuid4())`. The reference client and
> the `blemees` CLI's `open new` already do this; the `s_abc` /
> `s_xyz` strings throughout this spec are short placeholders for
> readability, not legal session ids.

#### 5.4.1 `options.claude.*`

| Field | CLI flag | Notes |
|---|---|---|
| `model` | `--model <v>` | |
| `system_prompt` | `--system-prompt <v>` | |
| `append_system_prompt` | `--append-system-prompt <v>` | |
| `tools` | `--tools <v>` | Empty string disables all tools. |
| `disallowed_tools` | `--disallowedTools <v...>` | |
| `permission_mode` | `--permission-mode <v>` | One of `default`, `acceptEdits`, `bypassPermissions`, `plan`. |
| `cwd` | `chdir()` before spawn | |
| `add_dir` | `--add-dir <v...>` | |
| `effort` | `--effort <v>` | |
| `agent` | `--agent <v>` | CC subagent name. Distinct from the top-level `backend` selector — this is the nested `options.claude.agent`. |
| `agents` | `--agents <json>` | CC subagent config map. |
| `mcp_config` | `--mcp-config <v...>` | |
| `strict_mcp_config` | `--strict-mcp-config` | |
| `settings` | `--settings <v>` | |
| `setting_sources` | `--setting-sources <v>` | |
| `plugin_dir` | `--plugin-dir <v>` (repeated) | |
| `betas` | `--betas <v...>` | |
| `exclude_dynamic_system_prompt_sections` | `--exclude-dynamic-system-prompt-sections` | |
| `max_budget_usd` | `--max-budget-usd <v>` | |
| `json_schema` | `--json-schema <v>` | |
| `fallback_model` | `--fallback-model <v>` | |
| `session_name` | `-n <v>` | |
| `session_persistence` | `--no-session-persistence` when `false` | |
| `include_partial_messages` | `--include-partial-messages` | |
| `include_raw_events` | n/a — translation-layer flag | When `true`, every `agent.*` frame the daemon emits for this session carries a `raw` field with the un-prefixed CC stream-json dict it was translated from. Default `false`. |
| `user_echo` | n/a — translation-layer flag | When `true`, the daemon emits `agent.user_echo` for the user's input message — internally maps to CC's `--replay-user-messages`. Default `false`; matches `options.codex.user_echo` so both backends are symmetric out-of-the-box. Tool-result events flow regardless. |

Flags the daemon refuses to pass (always rejected with `unsafe_flag`):
`--dangerously-skip-permissions`, `--allow-dangerously-skip-permissions`,
`--bare` (see note), `--continue`, `--from-pr`. Clients that need
bypassPermissions should set `permission_mode: "bypassPermissions"`
explicitly — the daemon allows that, it just refuses the legacy kill switch.

> **`--bare` note:** bare mode disables OAuth/keychain auth and requires
> `ANTHROPIC_API_KEY`. Incompatible with the daemon's typical auth
> assumption. v0.1 does not support it.

The daemon always passes `--verbose --input-format stream-json
--output-format stream-json`. Clients cannot override these — the
event multiplexer requires them.

#### 5.4.2 `options.codex.*`

These map directly to fields on `tools/call` arguments for the
`codex` (new session) / `codex-reply` (continue) MCP tools.

| Field | Codex tool argument | Notes |
|---|---|---|
| `model` | `model` | e.g. `gpt-5.2-codex`. |
| `profile` | `profile` | Profile name from `~/.codex/config.toml`. |
| `cwd` | `cwd` | Working directory for the session. |
| `sandbox` | `sandbox` | `read-only`, `workspace-write`, or `danger-full-access`. |
| `approval-policy` | `approval-policy` | `untrusted`, `on-failure`, `on-request`, `never`. |
| `base-instructions` | `base-instructions` | Replaces Codex's default base instructions. |
| `developer-instructions` | `developer-instructions` | Injected as a developer-role message. |
| `compact-prompt` | `compact-prompt` | Prompt used when compacting the conversation. |
| `config` | `config` | Free-form object, deep-merged over `~/.codex/config.toml`. |
| `include_raw_events` | n/a — translation-layer flag | When `true`, every `agent.*` frame carries the original `msg` dict from the underlying `notifications/codex/event` under `raw`. Default `false`. |
| `user_echo` | n/a — translation-layer flag | When `true`, the daemon forwards `item_completed{UserMessage}` as `agent.user_echo`. Default `false`; matches `options.claude.user_echo` so both backends are symmetric out-of-the-box. Tool-result events flow regardless. |

Backend-side process flags also passed by the daemon when relevant:

* `-c <key=value>` overrides — synthesised from `options.codex.config`.
* `--enable <feature>` / `--disable <feature>` — synthesised from
  `options.codex.config.features.<name>`.

The daemon does not expose `codex login` / `logout` / `mcp` (the *client*
subcommand) — those manage external state on the user's machine. The
codex backend assumes `codex login status` succeeds; otherwise sessions
fail with `auth_failed` (§5.10).

#### 5.4.3 Reply

Daemon reply on success:
```json
{
  "type":"blemeesd.opened",
  "id":"req_001",
  "session_id":"s_abc",
  "backend":"claude",
  "subprocess_pid":54321,
  "last_seq":0
}
```

`native_session_id` is the backend's own session identifier — present
**only when it differs from `session_id`**. Absence is the wire-level
signal "the daemon's session id is also the backend's id, use it
directly". For Claude this field is always omitted (CC's
`--session-id` accepts the daemon's value verbatim). For Codex it's
the `threadId` — omitted on a fresh open (unknown until the first
`session_configured` event), present on resume and after the first
turn. Clients usually don't need it but it appears in transcripts
and logs.

On failure:
```json
{"type":"blemeesd.error","id":"req_001","session_id":"s_abc","code":"spawn_failed","message":"..."}
```

### 5.5 User message

Client sends a new user turn to an open session. The frame shape is the
same regardless of backend; the daemon translates the inner `message`
into the backend's native input shape.

Simple text:
```json
{"type":"agent.user","session_id":"s_abc","message":{"role":"user","content":"Hello"}}
```

Multimodal (`content` may be an array of content blocks):
```json
{"type":"agent.user","session_id":"s_abc","message":{"role":"user","content":[{"type":"text","text":"What is in this image?"},{"type":"image","source":{"type":"base64","media_type":"image/png","data":"..."}}]}}
```

`message.role` must be `"user"`. `message.content` must be a string or
an array of content blocks. Any additional fields on `message` pass
through unchanged; the daemon does not validate them.

Per-backend translation:

* **Claude:** `message` is forwarded verbatim to `claude -p`'s
  stream-json stdin as `{"type":"user","message":<message>,"session_id":<native id>}`. Multimodal arrays go through unchanged — CC owns the
  inner block schema.
* **Codex:** the daemon issues a `tools/call` with `name:"codex"` (first
  turn) or `name:"codex-reply"` (subsequent turns) and
  `arguments:{prompt:<string>, threadId:<native id>?, ...static options from §5.4.2}`. If `content` is an array, text blocks are
  concatenated into the `prompt` string; non-text blocks (images,
  documents) are rejected with `invalid_message` because Codex's MCP
  tool surface does not yet accept them. This restriction is documented
  here; relaxing it is a future protocol addition (`agent.user.attachments`).

No `id` required. Responses stream as `agent.*` events until the turn
ends with an `agent.result`.

### 5.6 Event stream (daemon → client)

The daemon reads each line of the backend child's stdout, drives the
backend's native protocol (CC stream-json or Codex JSON-RPC), and
translates each native event into one or more `agent.*` frames. Frames
carry `session_id`, a monotonic per-session `seq`, and `backend`.

The full mapping is locked in
[`docs/agent-events.md`](docs/agent-events.md). The eight `agent.*`
types are:

* `agent.system_init` — first frame after spawn (model, cwd,
  capabilities).
* `agent.delta` — incremental output during a turn (`kind: "text" \| "thinking" \| "tool_input"`).
* `agent.message` — a complete assistant message.
* `agent.user_echo` — the backend's echo of the user's turn (CC and
  Codex both emit one).
* `agent.tool_use` — a tool invocation request from the model.
* `agent.tool_result` — the result the backend received for a tool
  invocation.
* `agent.notice` — informational backend events (rate-limit pings,
  Codex's MCP-startup chatter, etc.).
* `agent.result` — turn-end. Always the last frame for a turn.

Example (abridged) — same shape regardless of backend:
```json
{"session_id":"s_abc","seq":1,"type":"agent.system_init","backend":"claude","model":"claude-sonnet-4-6","tools":["Bash","Read","Edit"]}
{"session_id":"s_abc","seq":2,"type":"agent.delta","backend":"claude","kind":"text","text":"Hel"}
{"session_id":"s_abc","seq":3,"type":"agent.delta","backend":"claude","kind":"text","text":"lo"}
{"session_id":"s_abc","seq":4,"type":"agent.message","backend":"claude","role":"assistant","content":[{"type":"text","text":"Hello"}]}
{"session_id":"s_abc","seq":5,"type":"agent.result","backend":"claude","subtype":"success","duration_ms":1254,"num_turns":1,"usage":{"input_tokens":15,"output_tokens":1,"cache_read_input_tokens":0,"cache_creation_input_tokens":0}}
```

The daemon DOES translate (deduplicate, normalise field names, fold
sub-events). Clients that need backend-native fidelity opt in via
`options.<backend>.include_raw_events: true` to get the original event
under each frame's `raw` field.

Events that arrive on the backend child's **stderr** are wrapped and
forwarded as well (for visibility into warnings / auth errors):
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

Per-backend mechanism:

* **Claude:** the daemon sends SIGTERM to the `claude -p` child; if it
  is still alive after 500 ms, SIGKILL. It then respawns the
  subprocess with `--resume <session>` (all other flags identical to
  the original open).
* **Codex:** the daemon sends an MCP `notifications/cancelled` for the
  in-flight `tools/call` request id. The `codex mcp-server` child is
  *not* killed — it stays up and ready for the next turn.

Daemon emits:
```json
{"type":"blemeesd.interrupted","session_id":"s_abc"}
```

Any `agent.*` events emitted before the cancel are forwarded as normal.
Already-sent deltas are NOT retracted. The interrupted turn is closed
with `agent.result{subtype:"interrupted"}`.

Interrupt is a no-op (returns `blemeesd.interrupted` with `was_idle: true`) if
no turn is in flight.

### 5.8 Close

Explicit session close:
```json
{"type":"blemeesd.close","id":"req_099","session_id":"s_abc","delete":true}
```
- `delete: true` → daemon kills the subprocess and removes its **own**
  per-session state: the durable event log
  (`<event_log_dir>/<session>.jsonl`) and the usage sidecar
  (`<event_log_dir>/<session>.usage.json`). The backend's native
  transcript files are **not** touched —
  `~/.claude/projects/<cwd-hash>/<session>.jsonl` (Claude) and
  `~/.codex/sessions/.../rollout-*.jsonl` (Codex) live under
  directories the backends own, and Codex in particular tracks
  rollouts in an internal state DB; deleting behind its back surfaces
  as ERROR-level stderr noise on subsequent codex startups. Resume
  from disk (e.g. via `list_sessions` then `open … resume:true`)
  continues to work after a delete-close.
- `delete: false` (default) → daemon keeps its event log + usage
  sidecar so a later `resume:true` can replay across daemon
  restarts.

Either way, the backend's native transcript stays — clean it up
manually if you want it gone.

Daemon replies:
```json
{"type":"blemeesd.closed","id":"req_099","session_id":"s_abc"}
```

If the session has watchers attached (§5.14), they receive a
`blemeesd.session_closed{session_id, reason:"owner_closed"}`
notification *before* the daemon unhooks their writers. The closer
itself does **not** receive `session_closed` — it gets the `closed`
ack to its own request. Owners and watchers thus get distinct,
non-overlapping signals. `reason` is forward-extensible; v0.9 emits
only `"owner_closed"` (the explicit-close path), with future codes
reserved for connection-drop / reaper / crash paths.

### 5.9 Connection close

When the socket is closed from the client side without explicit `close`
messages (soft detach):

1. The writer attached to each session is unhooked immediately so no more
   frames are pushed to the dead socket.
2. **If a turn is in flight**, the subprocess is *not* killed — it keeps
   running to completion and the session is marked "finishing". Events
   continue to accumulate in the session's ring buffer (§5.11) and in
   the durable log if enabled, so a client that reconnects can replay
   them via `last_seen_seq`. When the backend next emits
   `agent.result`, the daemon gracefully terminates the child (Claude:
   close stdin and reap; Codex: close stdio and reap).
3. **If no turn is in flight**, the subprocess is terminated immediately
   (SIGTERM → 500 ms → SIGKILL).
4. Either way, the session record is *detached*, not deleted:
   `connection_id = None`, `detached_at = now()`. It is reapable after
   `IDLE_TIMEOUT` (during which a late-finishing turn will be torn down
   along with the session).
5. A new connection may reattach by opening the same `session` with
   `resume: true`, optionally passing `last_seen_seq` to catch up on
   anything it missed while disconnected.

Rationale: a hard kill mid-turn left the backend's on-disk transcript
in whatever partially-flushed state the kill signal allowed, silently
diverging the model's conversation state from what the client last
saw. Letting the turn complete closes the transcript cleanly and makes
mid-stream reconnects a replay problem, not a consistency problem.
The same policy applies to Codex (we don't `notifications/cancelled`
the in-flight call on plain disconnect — only on explicit
`blemeesd.interrupt`).

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
{"type":"blemeesd.error","id":"req_001","session_id":"s_abc","code":"backend_crashed","message":"stderr tail: ..."}
```

Error codes the client must handle:

| Code | Meaning | Fatal to connection? |
|---|---|---|
| `protocol_mismatch` | Incompatible protocol version. | Yes. |
| `invalid_message` | Malformed JSON or bad field. | No. |
| `unknown_message` | Unknown `blemeesd.*` type. | No. |
| `unknown_backend` | `blemeesd.open.backend` is not a backend the daemon knows. | No. |
| `unsafe_flag` | Client requested a refused flag. | No. |
| `session_unknown` | No such session. | No. |
| `session_exists` | Session id collides on open. | No. |
| `session_busy` | Another turn in flight. | No. |
| `spawn_failed` | Backend binary missing or launch failed. | No. |
| `backend_crashed` | Backend subprocess exited unexpectedly mid-turn (or, for Codex, the JSON-RPC channel returned a transport-level error). | No. |
| `auth_failed` | Backend reports it cannot authenticate (CC: OAuth token expired; Codex: not logged in / `OPENAI_API_KEY` missing). The daemon does not retry. | No. |
| `oversize_message` | Inbound frame too large. | Yes. |
| `slow_consumer` | Per-connection queue stalled. | Yes. |
| `daemon_shutdown` | Daemon shutting down. | Yes. |
| `internal` | Unexpected daemon error. | No. |

> `blemees/2` rename: the `blemees/1` codes `claude_crashed` and
> `oauth_expired` are gone. Crash reporting is unified under
> `backend_crashed`; auth failures (Anthropic OAuth, OpenAI API key
> missing, ChatGPT login lapsed) are unified under `auth_failed`.

### 5.11 Event stream durability (seq, ring buffer, replay)

Every outbound frame the daemon emits for a session — translated
`agent.*` events and synthetic `blemeesd.*` frames alike — carries a
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
  "daemon":"blemeesd/0.1.0","protocol":"blemees/2","pid":12345,
  "uptime_s":127.3,
  "socket_path":"/run/user/1000/blemeesd.sock",
  "backends":{"claude":"2.1.118","codex":"0.125.0"},
  "connections":3,
  "sessions":{
    "total":5,"attached":4,"detached":1,"active_turns":2,
    "by_backend":{"claude":3,"codex":2}
  },
  "config":{
    "ring_buffer_size":1024,"event_log_enabled":false,
    "idle_timeout_s":900,"shutdown_grace_s":30,
    "max_concurrent_sessions":64,"max_line_bytes":16777216
  }
}
```
No side effects. Forward-compatible: new fields may be added inside
`sessions` / `config` / `backends`, and new top-level keys may appear.
A backend missing from `backends` means the daemon could not detect
that binary at startup; sessions for it will fail with `spawn_failed`.

### 5.14 Watch (subscribe-only observer)

A second connection may subscribe to an existing session's event
stream without taking ownership. The owner keeps driving the session;
watchers receive the same `agent.*` events, `blemeesd.stderr`,
`blemeesd.error{backend_crashed,auth_failed}`, `blemeesd.replay_gap`,
and `blemeesd.session_closed` frames the owner does (where they apply
— `session_closed` is watcher-only), plus an optional replay on
subscribe.

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
`agent.user`, `blemeesd.interrupt`, `blemeesd.close`, and
`blemeesd.session_taken` remain connection-scoped to the owner.

When the owner explicitly closes the session, every watcher receives
`blemeesd.session_closed{session_id, reason:"owner_closed"}`
immediately before the daemon unhooks their writers — see §5.8.
Reattaching after that point returns
`blemeesd.error{code:"session_unknown"}`, so the close is the
watchers' canonical end-of-life signal.

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
  "backend":"claude",
  "native_session_id":"s_abc",
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

The accumulator is maintained from each `agent.result` event's
normalised `usage` block (see `NormalisedUsage` in
[`docs/agent-events.md`](docs/agent-events.md)). For Codex sessions
the reply also carries `cumulative_usage.reasoning_output_tokens`.
`context_tokens` is the sum of the last turn's input-side tokens
(fresh + `cache_read` + `cache_creation`) — compare to the model's
context window to gauge headroom.

**Persistence**: when `event_log_dir` is enabled, the counters are
written to `<event_log_dir>/<session>.usage.json` on every turn
(atomic rename) and reloaded on session reopen, so they survive
daemon restarts. Without the durable log they are in-memory only and
reset to zero on restart. `blemeesd.close {delete:true}` also
unlinks the sidecar.

### 5.16 Session listing (`list_sessions`)

Enumerate sessions known to the daemon. `cwd` and `live` are
independent, fully-composable filters; **omitting a filter means "no
filter on that axis."**

```json
{"type":"blemeesd.list_sessions","id":"req_6"}                                   // every session, every cwd
{"type":"blemeesd.list_sessions","id":"req_6","cwd":"/home/u/proj"}              // every session for that cwd
{"type":"blemeesd.list_sessions","id":"req_6","live":true}                       // live only, every cwd
{"type":"blemeesd.list_sessions","id":"req_6","live":false}                      // cold only, every cwd
{"type":"blemeesd.list_sessions","id":"req_6","cwd":"/home/u/proj","live":true}  // live only, that cwd
```

| `cwd` | `live`  | Behavior                                                                                                          |
|-------|---------|--------------------------------------------------------------------------------------------------------------------|
| set   | omitted | On-disk transcripts merged with live overlay for that cwd. Original v0.1 contract — parity with `/resume`.        |
| set   | `true`  | Live sessions only, scoped to that cwd. No disk scan.                                                              |
| set   | `false` | Cold (on-disk-only) sessions for that cwd. Excludes any session that's currently live.                             |
| absent| omitted | Every session, everywhere — full disk walk across `~/.claude/projects/*` and `~/.codex/sessions/*` plus every live session. |
| absent| `true`  | Every live session, all cwds. The cheap path for "what's running right now?". Suitable for a watch-mode picker.    |
| absent| `false` | Every cold session, all cwds. The historical-browser query.                                                        |

When `live:false` is set, sessions that *are* currently live are
subtracted from the result — even if their transcript is on disk.
The cold-only set is precisely the disk transcripts whose
`(backend, session_id)` is not present in `SessionTable`.

Reply:
```json
{
  "type":"blemeesd.sessions","id":"req_6",
  "cwd":"/home/u/proj",
  "sessions":[
    {
      "session_id":"5a01...",
      "backend":"claude",
      "attached":true,
      "cwd":"/home/u/proj",
      "model":"claude-sonnet-4-6",
      "title":"refactor utils.py",
      "started_at_ms":1745000000000,
      "last_active_at_ms":1745000123000,
      "owner_pid":12345,
      "last_seq":47,
      "turn_active":false
    },
    {
      "session_id":"older",
      "backend":"claude",
      "attached":false,
      "mtime_ms":1700000000000,
      "size":4321,
      "preview":"fix the bug"
    }
  ]
}
```

The reply echoes top-level `cwd` only when the request supplied one.
Each row in `sessions` is one of three shapes:

- **Live row** — `attached`, optional `cwd` / `model` / `title`,
  `started_at_ms`, `last_active_at_ms`, optional `owner_pid`,
  `last_seq`, `turn_active`. Surfaces the daemon's in-memory state.
- **Cwd-scoped on-disk row** — `mtime_ms`, `size`, optional
  `preview`. Built from a single project's transcript files. The
  request's top-level `cwd` is the implied per-row cwd.
- **All-cwds on-disk row** — same as above plus `cwd` (extracted
  from the transcript head, since the directory-name encoding is
  lossy) and, when readable, `model`. Self-describing because the
  reply has no top-level `cwd`.

If a session is both live and has a transcript, the row carries
fields from both groups (the live overlay merges into the disk row
by `(backend, session_id)`). Sort order is `last_active_at_ms`
(preferred, precise) falling back to `mtime_ms` (disk lag) when
absent. Unknown optional fields are **omitted**, never `null` —
clients should treat absence as "not known".

`title` is daemon-derived from the first observed user message,
capped at 80 characters. Sessions that have never driven a turn don't
have one. `owner_pid` is the SO_PEERCRED PID of the connection
currently driving the session, surfaced for audit/debugging; it is
absent when the session is detached or when the OS doesn't expose
peer credentials. Both fields exist primarily so multi-session UIs
(see [`blemees-tui`](https://github.com/blemees/blemees-tui)) can
build a watch-mode picker without scraping log files for ids.

**Cost note:** the no-filter form (`{}`) walks every project
directory and reads each transcript's head, which is O(total
sessions) across both backends. For users with thousands of
historical sessions this can take a while. Watch-picker UIs should
use `live:true` for the cheap variant; historical browsers should
expect to wait or paginate (Codex's walk is bounded by retention
caps, Claude's is not).

---

## 6. Backend Management

The daemon spawns one backend subprocess per open session. Backends
implement a small `AgentBackend` Protocol (`spawn`, `send_user_turn`,
`interrupt`, `close`, `build_resume_args`, `list_on_disk_sessions`,
`detect_auth_error`) so the dispatcher in §3 stays backend-agnostic.
The two implementations differ in argv construction, the wire on the
child's stdio, the resume mechanism, and on-disk transcript layout.

Both share the same per-session contract:

- One turn in flight at a time. A second `agent.user` while the
  backend has not yet emitted `agent.result` is rejected with
  `error{code:"session_busy"}`.
- Backend stdout / JSON-RPC frames are translated to `agent.*` per
  §5.6 and [`docs/agent-events.md`](docs/agent-events.md).
- Backend stderr is rate-limited and forwarded as `blemeesd.stderr`.
- Auth errors detected on the backend's diagnostic output surface as
  `auth_failed`; transport-level crashes as `backend_crashed`.

### 6.1 Claude backend (`backend:"claude"`)

#### 6.1.1 Launch invocation

Construct argv dynamically from `options.claude.*` (§5.4.1). Always
included:

```
claude -p
  --verbose
  --session-id <s>    OR    --resume <s>
  --input-format  stream-json   # fixed by the daemon; not client-settable
  --output-format stream-json   # fixed by the daemon; not client-settable
  [all other flags per §5.4.1 mapping, only when set]
```

Spawn context:
- `cwd` = `options.claude.cwd` or daemon cwd. Do a real chdir in the
  child (`asyncio.create_subprocess_exec(cwd=...)`).
- Inherit daemon env (carries `ANTHROPIC_TOKEN` /
  `CLAUDE_CODE_OAUTH_TOKEN` / `~/.claude/.credentials.json` access).
- stdin/stdout/stderr = `asyncio.subprocess.PIPE`.

#### 6.1.2 stdin — feeding user messages

Each client `agent.user` becomes one line on the subprocess stdin in
Claude Code's stream-json input shape. Canonical form for simple text:
```json
{"type":"user","message":{"role":"user","content":"<text>"},"session_id":"<native>"}
```
`content` arrays pass through unchanged:
```json
{"type":"user","message":{"role":"user","content":[...content blocks...]},"session_id":"<native>"}
```
Flush after each line.

#### 6.1.3 stdout — translating native events

The daemon reads stdout line-by-line, parses each line as JSON, runs
it through `translate_claude` to produce one or more `agent.*` frames,
and pushes each to the session. Non-JSON stdout is logged and dropped
(should not occur; indicates a CC bug). The daemon tracks the synthetic
`agent.result` to know when the turn has ended.

#### 6.1.4 Interrupt

Per §5.7:
- `subprocess.send_signal(SIGTERM)` (`proc.terminate()` on macOS/Linux).
- After 500 ms, if `proc.returncode is None`, `proc.kill()`.
- Await `proc.wait()` before respawn.
- Respawn uses the **same** stored launch argv from the original open, but
  with `--session-id X` replaced by `--resume X`.

#### 6.1.5 Session file management

Claude Code stores session state at
`~/.claude/projects/<cwd-hash>/<session-id>.jsonl`. The daemon does
not parse these files and does **not** delete them — that directory
is CC's to manage. `close{delete:true}` only removes the daemon's
own per-session state (event log + usage sidecar). See §5.8.

Optional startup housekeeping: remove session files older than
`SESSION_RETENTION_DAYS` (default 7). Opt-in via config.

### 6.2 Codex backend (`backend:"codex"`)

#### 6.2.1 Launch invocation

```
codex mcp-server
  [-c key=value]*       # synthesised from options.codex.config
  [--enable feature]*   # synthesised from options.codex.config.features.<name>=true
  [--disable feature]*  # synthesised from options.codex.config.features.<name>=false
```

Spawn context:
- `cwd` = `options.codex.cwd` or daemon cwd.
- Inherit daemon env (carries `OPENAI_API_KEY` and the ChatGPT-OAuth
  state under `~/.codex/auth.json`).
- stdin/stdout/stderr = `asyncio.subprocess.PIPE`.

The daemon performs the MCP `initialize` handshake immediately after
spawn (`protocolVersion: "2024-11-05"`, no client capabilities), waits
for the response, sends `notifications/initialized`, and lists tools
once to confirm `codex` and `codex-reply` are present. The
`session_configured` event from the first `tools/call` then drives the
synthesised `agent.system_init`.

#### 6.2.2 Driving turns — `tools/call`

Each `agent.user` becomes a JSON-RPC `tools/call` on the child's stdin.
First turn for a session uses the `codex` tool, subsequent turns the
`codex-reply` tool with the cached `threadId`:

```jsonc
// First turn
{"jsonrpc":"2.0","id":<n>,"method":"tools/call","params":{
  "name":"codex",
  "arguments":{
    "prompt":"<text>",
    /* options.codex.* — model, profile, cwd, sandbox, approval-policy,
       base-instructions, developer-instructions, compact-prompt, config */
  }
}}

// Continue
{"jsonrpc":"2.0","id":<n>,"method":"tools/call","params":{
  "name":"codex-reply",
  "arguments":{"prompt":"<text>","threadId":"<cached>"}
}}
```

`agent.user.message.content` arrays are flattened to a single string by
concatenating text blocks (§5.5); non-text blocks are rejected with
`invalid_message`.

The daemon stores the in-flight JSON-RPC `id` so it can match the
final response and drive cancellation.

#### 6.2.3 stdio — translating `notifications/codex/event`

Each `notifications/codex/event` notification carries
`_meta.{requestId,threadId}` and a `msg.{type,...}` body. The daemon
runs `msg` through `translate_codex` per
[`docs/agent-events.md`](docs/agent-events.md). The terminal frame is
synthesised from the JSON-RPC `result` (or `error`) for the originating
`tools/call`, augmented with the preceding `task_complete` and the last
`token_count`.

The daemon tracks the JSON-RPC response to know the turn has ended.

#### 6.2.4 Interrupt

The daemon sends an MCP cancel:
```json
{"jsonrpc":"2.0","method":"notifications/cancelled","params":{"requestId":<n>,"reason":"user_interrupt"}}
```

The `codex mcp-server` child stays running and ready for the next
`agent.user`. The interrupted turn produces
`agent.result{subtype:"interrupted"}`.

Codex 0.125.x typically responds to a cancel by emitting a
`codex/event{type:"turn_aborted"}` notification, and frequently does
*not* follow up with a JSON-RPC reply. The daemon finalises the
in-flight turn from whichever lands first — the abort event or the
JSON-RPC response — and drops late events tagged with the cancelled
turn's `_meta.requestId` so they don't pollute the next turn's
stream.

#### 6.2.5 Session file management

Codex writes a per-session rollout JSONL whose path the server reports
in `session_configured.msg.rollout_path` (e.g.
`~/.codex/sessions/2026/04/27/rollout-2026-04-27T14-42-22-019d…jsonl`).
The daemon caches this path so it can surface it on
`agent.system_init.capabilities.rollout_path`, but it does **not**
unlink the file on `close{delete:true}` — Codex tracks rollouts in
an internal state DB and deleting behind its back surfaces as
`state db returned stale rollout path …` ERROR-level stderr noise on
subsequent codex startups. See §5.8.

Date-bucketed enumeration for `blemeesd.list_sessions` walks
`~/.codex/sessions/` recursively (cheap; date-pruned to
`SESSION_RETENTION_DAYS` when discovery is enabled).

#### 6.2.6 Resume caveat (Codex 0.125.x)

In-process resume — same `codex mcp-server` child, sequential turns
via `codex-reply` with the cached `threadId` — works as expected.

Cross-process resume is unstable on Codex 0.125.x: opening a fresh
`codex mcp-server` child and calling `codex-reply` with a prior
`threadId` returns a successful but empty result. The daemon issues
`codex-reply` with the cached id correctly; codex itself does not
rehydrate model-side state. Treat resume across daemon restarts /
reconnects as best-effort on Codex until upstream fixes it. Claude
resume preserves context across reattach; Codex does not.

---

## 7. Security

- **Socket path:** `$XDG_RUNTIME_DIR/blemeesd.sock` on Linux. On macOS, which
  lacks `$XDG_RUNTIME_DIR`, use `/tmp/blemeesd-$UID.sock`. Configurable via
  `--socket`.
- **Permissions:** socket created with mode `0600`. If the path exists on
  startup and is not owned by the current UID, refuse to start.
- **No authentication beyond socket perms.** Anyone who can `connect()` the
  socket gets full access to every backend the daemon can reach (the
  user's Claude subscription, ChatGPT/OpenAI account, etc.).
- **No remote access.** No TCP listener. For remote use, forward via SSH.
- **Peer identity:** the daemon captures `SO_PEERCRED` (Linux) /
  `LOCAL_PEERCRED` (macOS) at connect time and logs peer PID/UID.
  Informational only; no enforcement in v0.1.
- **Secret handling:** `options.<backend>.system_prompt` /
  `base-instructions` / `developer-instructions`, `agent.user` content,
  and event deltas are never logged at INFO+. At DEBUG, bodies are
  redacted to `<redacted N chars>`. OAuth / API tokens are never logged.

---

## 8. Configuration

Config file (optional): `~/.config/blemeesd/config.toml`. CLI flags and env
vars override. Env prefix: `BLEMEESD_`.

| Key | CLI flag | Env var | Default |
|---|---|---|---|
| `socket_path` | `--socket` | `BLEMEESD_SOCKET` | `$XDG_RUNTIME_DIR/blemeesd.sock` |
| `claude_bin` | `--claude` | `BLEMEESD_CLAUDE` | `claude` on PATH |
| `codex_bin` | `--codex` | `BLEMEESD_CODEX` | `codex` on PATH |
| `log_level` | `--log-level` | `BLEMEESD_LOG_LEVEL` | `info` |
| `log_file` | `--log-file` | `BLEMEESD_LOG_FILE` | stderr |
| `max_line_bytes` | — | `BLEMEESD_MAX_LINE` | `16777216` |
| `idle_timeout_s` | — | `BLEMEESD_IDLE_TIMEOUT` | `900` |
| `session_retention_days` | — | — | `7` (0 disables) |
| `max_sessions_per_connection` | — | — | `32` |
| `max_concurrent_sessions` | — | — | `64` |
| `stderr_rate_lines` | — | — | `50` |
| `stderr_rate_window_s` | — | — | `10` |

A backend whose binary cannot be located at startup is simply omitted
from the `blemeesd.hello_ack.backends` map; the daemon serves whichever
backends *are* available. Sessions for a missing backend fail with
`spawn_failed` at open time.

CLI:
```
blemeesd [--socket PATH] [--claude PATH] [--codex PATH]
        [--log-level LEVEL] [--log-file PATH]
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

`blemeesd` is a **per-user** daemon by design — one instance per UID,
one set of upstream agent accounts per instance (the `claude` and
`codex` CLIs read state out of the user's home directory), socket
pinned to that UID. Every install path above registers it with a
per-user service manager, not a system one.

**macOS — LaunchAgent.** `brew services start blemees` writes
`~/Library/LaunchAgents/homebrew.mxcl.blemees.plist` and loads it into your
GUI session via `launchctl`. Manual install writes
`~/Library/LaunchAgents/com.blemees.blemeesd.plist`. Either way it:

- starts at login, restarts on crash (`KeepAlive`),
- stops at logout (a power cycle with no login leaves it off),
- runs as you, so `~/.claude/` and `~/.codex/` creds and session
  logs are yours.

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

#### Finding the `claude` and `codex` binaries

Services do **not** inherit your shell's `PATH`. `brew services` and
systemd `--user` start with a minimal `PATH` (`/usr/bin:/bin:...`) plus
whatever the unit file adds. The tap formula extends it to cover
`~/.local/bin`, `~/bin`, and `$HOMEBREW_PREFIX/bin`, which is where the
standalone installers put `claude` and `codex`. The symptom when this
is wrong is a healthy `daemon.start` line but every session for the
affected backend ending in `spawn_failed`.

If your `claude` or `codex` lives elsewhere (npm global under
`~/.nvm/...`, a custom path, etc.), override with `BLEMEESD_CLAUDE`
and / or `BLEMEESD_CODEX`:

- macOS:
  ```bash
  launchctl setenv BLEMEESD_CLAUDE "$(which claude)"
  launchctl setenv BLEMEESD_CODEX  "$(which codex)"
  brew services restart blemees
  ```
  `launchctl setenv` persists until reboot; for durable override, add
  an `EnvironmentVariables` block to the plist.
- Linux:
  ```bash
  systemctl --user edit blemeesd
  # add in the editor:
  #   [Service]
  #   Environment="BLEMEESD_CLAUDE=/full/path/to/claude"
  #   Environment="BLEMEESD_CODEX=/full/path/to/codex"
  systemctl --user restart blemeesd
  ```

Or bake `--claude /full/path/to/claude --codex /full/path/to/codex`
into the unit's `ExecStart` / plist `ProgramArguments`.

#### Running at boot

You probably do not want this — `claude` and `codex` run with whatever
privileges the daemon has, and a broader trust boundary means a bigger
blast radius. If you need it anyway (e.g. headless server, unattended
box), these are the supported paths. Both keep the daemon running as
**one named user**; do not run it as root.

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

### 9.1 Backend crash mid-turn
On EOF on the child's primary stream (CC stdout closed, or Codex stdio
closed before responding) or a non-zero exit during a turn:
```json
{"type":"blemeesd.error","session_id":"s_abc","code":"backend_crashed","message":"<stderr tail>"}
```
Session remains open. Next `agent.user` respawns the child (Claude:
relaunches `claude -p --resume <s>`; Codex: relaunches `codex
mcp-server` and replays via `codex-reply` with the cached `threadId`).

### 9.2 Auth failure
Each backend has its own detection signatures:

* **Claude** — patterns in stderr: `401`, `OAuth token expired`,
  `Please run claude auth`, `Session authentication failed`. The user
  must run `claude auth`.
* **Codex** — JSON-RPC `error` with auth-related code, or stderr
  patterns indicating missing `OPENAI_API_KEY` / lapsed ChatGPT login.
  The user must run `codex login`.

Either surfaces as:
```json
{"type":"blemeesd.error","session_id":"s_abc","code":"auth_failed","backend":"claude","message":"Run `claude auth` to re-authenticate."}
```
Do not retry automatically. Subsequent user messages repeat the error
until the user re-auths and the daemon sees a successful spawn /
turn.

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
`agent.result` before being terminated, so their transcripts close
cleanly.

1. Stop accepting new connections.
2. Emit `error{code:"daemon_shutdown"}` on every live connection.
3. For every session with `turn_active=True`, set `_finishing=True`.
   Events continue to accumulate in the ring buffer and (if enabled)
   durable log, so a client that reconnects to a restarted daemon can
   replay them via `last_seen_seq`.
4. Wait up to `shutdown_grace_s` seconds (default 30) for finishing
   sessions to reach their next `agent.result` and self-terminate.
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
- Never log `system_prompt` / `base-instructions` /
  `developer-instructions`, `agent.user.message.content`, event
  deltas, or stderr subprocess output bodies at INFO+. At DEBUG,
  redact to `<redacted N chars>`.
- INFO events to include:
  - daemon start/stop (socket path, detected backends + versions, pid)
  - connection open/close (peer pid, uid)
  - session open/close (backend, model, resume flag — NOT prompts)
  - subprocess spawn/exit (backend, pid, exit code)
  - error frames emitted
  - interrupt received

---

## 11. Testing Requirements

### 11.1 Unit (no backends required)
- `test_protocol.py`: encode/decode every message type; malformed inputs;
  oversize frames; UTF-8 edge cases (surrogate pairs, NUL bytes).
- `test_session.py`: session table lifecycle; idle-timeout reaper;
  reattach by session id; delete-on-close.
- `test_translate_claude.py`, `test_translate_codex.py`: pure
  translator tests against fixture frames captured from real backends
  (under `docs/traces/` and copied into `tests/fixtures/`). One row
  per row of the table in [`docs/agent-events.md`](docs/agent-events.md).

### 11.2 Mock-backend tests
Two stub binaries:

- `fake_claude.py` — reads stream-json on stdin and emits scripted
  stream-json events on stdout.
- `fake_codex.py` — speaks JSON-RPC 2.0 on stdio with a scripted
  `initialize` / `tools/list` / `notifications/codex/event` /
  `tools/call` response sequence.

Coverage applies to both backends:
- Full turn → `agent.result` → next `agent.user` works again.
- Crash mid-turn → `backend_crashed`, next turn respawns.
- Interrupt → backend-appropriate cancel observed, continues.
- Concurrent sessions (3 parallel, mixed backends) do not interfere.
- Resume mapping is correct (CC: `--session-id` vs `--resume`; Codex:
  `codex` vs `codex-reply` with cached `threadId`).
- Unsafe flags (`options.claude.dangerously_skip_permissions`,
  `options.codex.config.<refused>`) are rejected at the
  `blemeesd.open` stage.
- `backend:"unknown"` is rejected with `unknown_backend`.

### 11.3 End-to-end tests
Two pytest marks, applied separately so a developer can run only the
backends installed on the machine:

- `requires_claude` — skipped unless `claude` is installed and
  authenticated. Same scenarios as before:
  - Turn → text response, `agent.result` seen.
  - Context preserved across two turns in one connection.
  - Close → reattach with `resume: true` → context intact.
  - Interrupt mid-generation → respawn → continuation works.
- `requires_codex` — skipped unless `codex` is installed and
  `codex login status` reports logged in. Same scenarios.

### 11.4 Latency benchmarks (`python -m blemees.bench --backend {claude,codex}`)
Acceptance targets on an ordinary dev machine:
- **Claude:** cold open → first event ≤ 1.5 s, warm user → first event ≤ 0.5 s, resume open → first event ≤ 1.5 s.
- **Codex:** initialize handshake adds a fixed cost. Warm-user → first
  delta target is ≤ 1.0 s; cold open + initialize budget is documented
  empirically rather than gated.

---

## 12. Versioning

- Protocol: `blemees/2` in v0.1. Breaking changes bump to `blemees/3`.
  The daemon supports a **single** protocol version at a time; clients
  must request the version the daemon advertises in `hello_ack`.
  `blemees/1` is gone — pre-1.0 means no compatibility shims.
- Daemon: semver. `0.x` unstable; breaking changes allowed pre-1.0.

### Notable changes in 0.9.0

- **`blemeesd.list_sessions` filters compose** (§5.16). `cwd` is now
  optional, `live` is a new optional boolean. The reply's
  `SessionSummary` shape is extended with live-only optional fields
  (`title`, `model`, `cwd`, `started_at_ms`, `last_active_at_ms`,
  `owner_pid`, `last_seq`, `turn_active`). Clients on the original
  shape still work — every old field is still emitted, every new
  field is optional.
- **`blemeesd.session_closed`** (§5.8, §5.14) — new outbound frame
  delivered to watchers when the owner closes a session.
- **`blemees` console_script renamed to `blemeesctl`** (§0). The
  daemon wheel no longer ships a `blemees` command; that name now
  belongs to the chat TUI shipped by the
  [`blemees-tui`](https://github.com/blemees/blemees-tui) package.
  Users who typed `blemees` for the wire-protocol REPL should
  retrain to `blemeesctl`. No deprecation alias — clean break, since
  any alias would have collided with the TUI's claim on the name.

---

## Appendix A: Reference client example

The same event-loop body works for either backend — the only thing
that changes is the `backend` selector and the matching `options`
block.

```python
import asyncio, uuid
from blemees.client import BlemeesClient

async def stream_one_turn(backend: str, options: dict, prompt: str) -> None:
    async with BlemeesClient.connect() as c:
        async with c.open_session(
            session_id=str(uuid.uuid4()),
            backend=backend,
            options={backend: options},
        ) as sess:
            await sess.send_user(prompt)
            async for event in sess.events():
                t = event.get("type")
                if t == "agent.delta" and event.get("kind") == "text":
                    print(event["text"], end="", flush=True)
                elif t == "agent.result":
                    print()
                    break
                elif t == "blemeesd.error":
                    raise RuntimeError(event["message"])

async def main():
    await stream_one_turn(
        "claude",
        {
            "model": "sonnet",
            "system_prompt": "You are a terse assistant. Answer in one sentence.",
            "tools": "",
            "permission_mode": "bypassPermissions",
            "cwd": "/home/u/proj",
        },
        "What is 2+2?",
    )
    await stream_one_turn(
        "codex",
        {
            "model": "gpt-5.2-codex",
            "sandbox": "read-only",
            "approval-policy": "never",
            "cwd": "/home/u/proj",
        },
        "What is 2+2?",
    )

asyncio.run(main())
```
