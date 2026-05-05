# Test follow-ups

Categories of tests we explicitly chose **not** to land in the comprehensive
e2e push (claude/codex). They're skipped because each one costs a chunk of
plumbing — short-lived daemon config, fault injection, or expensive runtime
budgets — that didn't fit the bug-hunting cadence we were on.

Ordered roughly by ROI: how likely the test is to find a real defect divided
by how much scaffolding it needs.

---

## High value, moderate scaffolding

### 1. `IDLE_TIMEOUT` reaper

A detached session is reaped after `idle_timeout_s` of no activity (spec
§5.9 / §3 — "Unattached sessions reaped after `IDLE_TIMEOUT` (default
900 s)"). Mock-tested via `SessionTable.reap_idle()`; not exercised e2e.

**What to verify:**
- Open a session, run a turn, close the connection (soft detach).
- Wait `idle_timeout_s + 1`.
- New connection asks `session_info` → `session_unknown` (reaped from memory).
- The on-disk transcript persists (reaper doesn't `delete_file=True`).

**Plumbing:** custom `Daemon` fixture with `idle_timeout_s=2`. Each test
takes ~3 s. Use the existing `tmp_path.resolve()` cwd pattern so the
on-disk transcript ends up under pytest's tmp dir.

### 2. Replay gap when `last_seen_seq` is too old

Ring buffer default is 1024 frames per session
(`BLEMEES_AGENTD_RING_BUFFER_SIZE`). When a reattach asks for `last_seen_seq`
that's older than the oldest buffered frame, the daemon emits
`agent.replay_gap{since_seq, first_available_seq}` once before live
delivery (spec §5.11).

**What to verify:**
- Open a session, send `last_seen_seq=0`, then drive enough turns to
  push the ring past 1024 frames.
- Reconnect with `last_seen_seq=1`. Expect a single `replay_gap` frame
  followed by replay starting at `first_available_seq`.

**Plumbing:** spawn a `Daemon` with `BLEMEES_AGENTD_RING_BUFFER_SIZE=4` (or
config-equivalent) so 5 trivial turns blow the ring. Real claude can
generate 4+ frames in a single turn (system_init + delta + message +
result) so this might land in 1–2 turns. With small ring sizes the
test is cheap.

### 3. `agent.notice` categories from claude

Real CC emits notices for cache events, MCP startup chatter,
rate-limit pings (and codex emits its own set: `mcp_startup_*`,
`rate_limits`, etc.). We never assert on these. They're translator
output — silently regressing the mapping table would be hard to
notice.

**What to verify:**
- Run a real turn against each backend with `include_raw_events=True`.
- Filter for `agent.notice` frames. Snapshot test the `category`
  field set (e.g., must include at least one `task_started` for codex,
  at least one cache-related notice for claude when caches are warm).

**Plumbing:** none beyond existing helpers; just an extra assertion
on the existing turn-flow tests.

### 4. Daemon graceful shutdown (SIGTERM)

Spec §9.5: SIGTERM stops accepting new connections, lets in-flight
turns finish for `shutdown_grace_s` (default 30 s), then force-kills.
Mock-tested. Real backend behavior (especially codex, which can take
minutes for `tools/call` to settle) is the interesting case.

**What to verify:**
- Open a session, send `agent.user`, send SIGTERM to the daemon mid-turn.
- Daemon emits `agent.error{code:daemon_shutdown}` to live
  connections.
- The in-flight turn is allowed to complete; the durable event log
  (if enabled) gets the final `agent.result`.

**Plumbing:** `Daemon` with short `shutdown_grace_s=5` (so the test
doesn't hang for 30 s when the model takes a while), real
`os.kill(os.getpid(), signal.SIGTERM)` from inside the test? Easier:
expose `daemon.request_shutdown()` (already used by the fixture
teardown) and verify the same wire behavior.

---

## High value, expensive plumbing

### 5. `backend_crashed` mid-turn

Spec §9.1: EOF on the child's stdio (or non-zero exit during a turn)
surfaces as `agent.error{code:backend_crashed, message:"stderr
tail: …"}`. Mock-tested via `fake_claude.py crash` mode. Real-backend
version requires injecting a kill on the subprocess.

**Approach:**
- Open a session with claude, start a long turn.
- Reach into the daemon via the test's `Daemon` instance to find the
  session's `backend.proc.pid`, then `os.kill(pid, signal.SIGKILL)`.
- Expect `agent.error{code:backend_crashed}` plus a session that
  respawns transparently on the next `agent.user` (spec §9.1).

**Risk:** brittle to internal API changes (poking at `daemon._sessions`
to get the pid). Worth it because backend_crashed is the one error
code that's hard to test without fault injection.

### 6. `auth_failed`

Spec §9.2: each backend has its own auth detection. Claude greps
stderr for `401`, `OAuth token expired`, etc. Codex parses JSON-RPC
errors with auth-related codes.

**Approach (claude):** Run with a dummy `BLEMEES_AGENTD_CLAUDE` script that
prints "401: Unauthorized" to stderr and exits non-zero. Daemon
should emit `agent.error{code:auth_failed, message:"Run \`claude
auth\` …"}` and not retry.

**Approach (codex):** Similar — a fake binary that returns a JSON-RPC
error with an auth-related code. Or `unset OPENAI_API_KEY` + remove
`~/.codex/auth.json` (destructive, don't do this in tests).

The fake-binary approach is the cleanest. Could share scaffolding
with `tests/blemees/fake_claude.py` / `fake_codex.py` by adding new
`auth` modes (already half-done — `fake_claude.py` has an `auth`
mode; pull it through into a real-style e2e).

### 7. `slow_consumer`

Spec §9.3: per-connection event queue is bounded (1024). When full
and not drained for `_SLOW_CONSUMER_TIMEOUT_S` (default 30 s), the
daemon emits `agent.error{code:slow_consumer}` and force-closes
the connection.

**Approach:**
- Open a session, kick off a turn that produces lots of frames
  (e.g., a `Count to 500` prompt or `include_raw_events=True` with a
  long prompt).
- On the test side, **stop reading** the socket but keep it open.
- After 30 s, expect the daemon to emit `slow_consumer` and close.

**Cost:** ~35 s per run. Pin to `BLEMEES_AGENTD_SLOW_CONSUMER_TIMEOUT=2`
(if exposed) to make it cheap.

### 8. `oversize_message`

Spec §5.1 / §9.x: line >`max_line_bytes` (default 16 MiB) → close
connection with `agent.error{code:oversize_message}`.

**Approach:** Open a connection, send a single `agent.user` frame
with `content` ≥ 17 MiB.

**Cost:** mostly disk/memory pressure; the test itself is fast.
Lower `max_line_bytes` via config to 1 KiB to make it trivial.

---

## Backend-specific

### 9. Codex 0.125.x `interrupt_then_continue` (currently deselected)

The follow-up turn after an interrupt routinely takes >5 minutes for
codex 0.125.x to finalize. The existing test
`test_real_codex_interrupt_then_continue` is in the file but
deselected via `--deselect`. If/when codex stabilises this path,
re-enable it. Until then, it's a known-flake.

### 10. Codex tool use (`exec_command_*`)

Codex backend's translator maps `exec_command_*` → `agent.tool_use` /
`agent.tool_result`. Mapping is *pencilled in but not trace-verified*
(per `docs/codex-backend-plan.md`). Need a real prompt that triggers
shell exec under the read-only sandbox.

**Plumbing:** prompt like "tell me the current date by running
`date`". Codex with `sandbox: workspace-write` and `approval-policy:
never` (since `read-only` doesn't allow exec). Capture the agent.*
frames and assert on the tool flow.

### 11. Codex `profile` and `config` knobs

`options.codex.profile` reads a profile from `~/.codex/config.toml`.
`options.codex.config` is a free-form dict that gets `-c key=value`
flattened onto codex's argv, including `features.<name>=true/false`
→ `--enable feature` / `--disable feature`.

**Plumbing:** snapshot test against the codex argv (similar to
`argv_trace_path` in the mock suite, but for real codex). Trickier
since real codex won't expose its own argv. Maybe inspect via
`/proc/<pid>/cmdline` on Linux only.

### 12. `include_raw_events` for codex

Equivalent to the claude test we already have — open with
`include_raw_events: true`, verify each `agent.*` frame carries a
`raw` field with the un-prefixed `notifications/codex/event` `msg`
dict.

**Plumbing:** none — straight clone of the claude test.

---

## Schema / wire-protocol edges

### 13. Surrogate pairs, NUL bytes, BOM in content

`test_protocol.py` (mock suite) already covers UTF-8 edge cases at
the codec level. The remaining e2e question: does claude / codex
cope with these in `agent.user.message.content`? They probably do
(both APIs are UTF-8) but worth a smoke test.

### 14. `max_sessions_per_connection` / `max_concurrent_sessions`

Config caps:
- `max_sessions_per_connection` (default 32)
- `max_concurrent_sessions` (default 64)

Open until the cap is hit, expect `session_exists` (already used for
"max reached"). Mock-tested probably; e2e value is just confirming
the cap actually fires under real spawn cost.

### 15. Connection-level frame interleaving stress

Open many sessions on one connection, drive them all in parallel,
plus pings/status interleaved. Confirms the writer queue doesn't
reorder frames or starve.

**Cost:** moderate — N parallel claude turns at once isn't free.
Maybe scope to a small N (3-5) and a single quick prompt each.

---

## Observability / logs

### 16. Structured-log assertions

The daemon emits structured JSON logs (spec §10) at INFO and DEBUG.
None of the existing tests inspect log output. A future test could
configure the daemon's log handler to a `StringIO`, run a turn, and
assert on the presence/absence of certain events (especially the
secret-redaction guarantees in §7).

---

## Notes on cost

The current full e2e suite runs in ~3 min per backend on a recent
Mac (haiku for claude, gpt-5.2-codex for codex). The follow-ups
above would add:

| Category | Approx wall-clock |
|---|---|
| Idle reaper (3-5 tests, 2-3 s each) | +15 s |
| Replay gap (1 test, ring=4) | +5 s |
| Notices (folded into existing) | +0 s |
| Graceful shutdown (1-2 tests) | +20 s |
| backend_crashed (1 test) | +10 s |
| auth_failed (2 tests, fake binaries) | +5 s |
| slow_consumer (1 test, timeout=2) | +5 s |
| oversize_message (1 test) | +1 s |
| Codex tool use (1-2 tests) | +60-120 s |
| Codex profile/config (1 test) | +10 s |
| Codex include_raw_events (1 test) | +15 s |
| Schema/UTF-8 edges (3-5 tests) | +30 s |
| Connection caps (2 tests) | +20 s |
| Stress (1 test) | +30 s |
| Log assertions (2-3 tests) | +30 s |

Total budget for a follow-up round: roughly +5 minutes wall-clock
plus a few hours of plumbing for the fault-injection scaffolding
(items 5–7 mostly).
