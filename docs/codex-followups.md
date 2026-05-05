# Codex backend follow-ups (post-Phase 6)

Three open questions remaining after Phases 0-5 of the Codex backend
plan. None block tagging a release; all should be revisited before the
codex backend is treated as stable.

The summary of each item lives in
[`docs/codex-backend-plan.md`](codex-backend-plan.md) under "Risks &
open questions"; this doc is the deeper write-up grounded in the
current code, with concrete suggestions for the fix.

---

## 1. Codex tool-call event shape (`exec_command_*`) is not trace-verified

### What it is

When the model running inside Codex decides to run a shell command
(`ls`, `grep`, `pytest …`), `codex mcp-server` emits stream events
describing the invocation and its output. The blemees daemon
translates those into the unified `agent.tool_use` /
`agent.tool_result` vocabulary so a client app can handle tool calls
identically across backends.

### Current state

`blemees/backends/translate_codex.py:_translate_exec_command` assumes
the Codex envelope shape:

| Codex field (assumed) | Where it lands on `agent.tool_use` |
|---|---|
| `msg.call_id` | `tool_use_id` |
| `msg.command` (or `msg.params`) | `input` |
| `msg.tool` | `name` (defaults to `"shell"`) |

| Codex field (assumed) | Where it lands on `agent.tool_result` |
|---|---|
| `msg.call_id` | `tool_use_id` |
| `msg.output` | `output` |
| `msg.is_error` | `is_error` |

These names came from reading Codex's open-source code, **not** from a
captured trace. The trace under `docs/traces/` was captured with
`prompt: "Reply with exactly: pong."` — the model never executed a
shell command, so we never saw a real `exec_command_begin` /
`exec_command_end` envelope on the wire.

### Failure modes

Three things could be wrong without us noticing:

- **Field names off by one rename.** Codex's source might call the
  field `command` in one place and `argv` on the wire. The translator
  silently drops or mistypes it.
- **The events might not be called `exec_command_*` at all on this
  MCP version.** Some Codex builds emit `mcp_tool_call_*` or similar.
  If so, the prefix-match (`msg_type.startswith("exec_command_")`)
  never fires and the entire tool-use stream falls through to the
  "unknown event → notice" fallback. Clients see `agent.notice` rows
  instead of the structured `agent.tool_use` / `agent.tool_result`
  pair they expect.
- **`exec_command_output_delta` is currently a notice.** If Codex
  streams stdout chunks through that, we ought to convert them to
  `agent.delta{kind:"text"}` (or a tool-output-specific delta) rather
  than dropping them into a generic notice.

The mock test (`test_exec_command_begin_to_tool_use`) validates the
translator against fabricated input. It tells us nothing about
whether the real Codex matches that shape.

### How to verify

1. **Capture a real trace.** Edit `scripts/codex_trace.py` to use a
   tool-using prompt:

   ```python
   "prompt": "Run `ls -la` and tell me the largest file.",
   "approval-policy": "never",
   "sandbox": "workspace-write",
   ```

   Run `python scripts/codex_trace.py --phase turn` to land a fresh
   `docs/traces/codex-mcp-turn-<ts>.jsonl` with the actual envelopes
   on disk.

2. **Compare against the translator.** Open the trace, find the events
   bracketing the shell command, and check three things against the
   current expectations:
   - Does the event type literally start with `exec_command_`?
   - Are the field names (`call_id`, `command`, `tool`, `output`,
     `is_error`) actually present?
   - Are there events between begin/end (`exec_command_output_delta`?)
     we should be promoting to `agent.delta` instead of `agent.notice`?

3. **Lock the contract.** Add a fixture-driven test in
   `tests/blemees/test_translate_codex.py` that loads the captured
   line and asserts the resulting frame — same pattern as the
   `session_configured` / `agent_message_content_delta` tests. After
   that, the row in `docs/agent-events.md` for `exec_command_*` can
   drop its caveat.

---

## 2. Codex rollout `cwd` field is documented as optional

### What it is

Codex writes a JSONL transcript of every session to disk. The path
embeds year/month/day/timestamp/threadId:

```
~/.codex/sessions/2026/04/27/rollout-2026-04-27T14-42-22-019dd03f-…dae.jsonl
```

The blemees daemon enumerates these files when a client asks
`agent.list_sessions{cwd: "/work/repo"}` so the user can pick a
prior session to resume. The daemon doesn't write rollouts itself —
Codex does that natively.

### Current state

`blemees/backends/codex.py:list_on_disk_sessions(cwd)`:

1. Walks `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`, newest day first.
2. For each rollout file, reads the first ~16 JSON lines.
3. Looks for a `session_configured` event among those lines.
4. Extracts `session_configured.cwd`.
5. **If `cwd` is missing or doesn't match, drops the row silently.**

```python
rollout_cwd = sc.get("cwd") if isinstance(sc, dict) else None
if not isinstance(rollout_cwd, str) or rollout_cwd != cwd:
    continue
```

### Failure modes

Codex's `session_configured` event treats `cwd` as **optional**. The
trace we captured had it, so the missing-`cwd` branch was never hit
in tests. Two scenarios produce missing-`cwd` in the wild:

- **Old rollouts.** A user with rollouts from before Codex started
  writing `cwd` (or from a build that omits it) gets an empty list
  back. No log; just silence.
- **Codex omits `cwd` for default-cwd sessions.** A reasonable future
  optimisation; would silently break the picker for any session
  started that way.

A subtler edge case: **path normalisation**. The client passes its
`os.getcwd()` and Codex stamps whatever it received. If one is
`/Users/me/proj` and the other is `/private/Users/me/proj` (macOS
firmlink), or one has a trailing slash, the equality check fails
silently.

The user-visible symptom is *"my old codex sessions don't appear in
the picker"* — no error, no warning, just a shorter list than
expected.

### Suggested fix

Three small parts, in order of effort:

1. **Don't drop rollouts that lack `cwd`.** Treat missing `cwd` as
   "include in every cwd's listing" rather than "exclude from all."
   Tag the row with `"cwd_known": false` so a client can choose to
   dim/hide it.

2. **Normalise paths.** `os.path.realpath(p).rstrip("/")` on both
   sides before comparing.

3. **Try fallback keys.** Some Codex builds may use `working_directory`
   or nest under `capabilities`. A defensive extractor that tries a
   couple of keys before giving up costs nothing.

Sketch:

```python
def _rollout_cwd(sc: dict) -> str | None:
    for key in ("cwd", "working_directory"):
        v = sc.get(key)
        if isinstance(v, str) and v:
            return os.path.realpath(v).rstrip("/")
    return None

def _cwd_matches(rollout_cwd: str | None, requested: str) -> bool:
    if rollout_cwd is None:
        return True   # don't hide rollouts with unknown cwd
    return rollout_cwd == os.path.realpath(requested).rstrip("/")
```

### How to verify

- **Synthetic.** Add a test in
  `test_backend_codex.py::test_list_on_disk_sessions_*` that writes a
  rollout whose `session_configured` line omits `cwd` entirely and
  asserts `list_on_disk_sessions(cwd)` still returns it.
- **Empirical.** Point `list_on_disk_sessions` at a real
  `~/.codex/sessions/` and compare the count of returned rows against
  `find ~/.codex/sessions -name 'rollout-*.jsonl' | wc -l`.

### Why this hasn't bitten yet

Phase 4 tests against rollouts the suite itself wrote, with `cwd`
always present. The real-codex e2e never exercised `list_sessions`
because each test creates a fresh session. The first user with a
year-old `~/.codex/sessions` tree will hit it.

---

## 3. `raw` payload size for codex is unbounded

### What it is

When a client opens a session with
`options.<backend>.include_raw_events: true`, every translated
`agent.*` frame carries an additional `raw` field with the original
native event. Useful for debug tooling; opt-in because it ~doubles
on-the-wire bytes.

Two daemon-side stores hold those frames durably:

- The per-session **ring buffer** (`blemees/event_log.py:RingBuffer`)
  — fixed at 1024 frames per session. Used for replay-on-reattach.
- The optional **durable event log** (`DurableEventLog`) —
  append-only `<event_log_dir>/<session>.jsonl` if the daemon is
  started with `--event-log-dir`. No size cap, one file per session.

Both store the post-translation frame, including its `raw` field if
the option was set at open time.

### Why codex is different

Claude Code's native events are pre-summarised: one `stream_event` =
one short delta. The translator's output is roughly the same size as
the native event, so `raw` overhead is ~1×.

Codex's events are wildly chatty. Concrete numbers from the captured
trace (one-turn `pong` reply, ~19.5 KB total):

- `raw_response_item` shows up 5 times. Sizes: 304, 315, 502, 1309,
  **5991** bytes for one line. The 5991-byte line carries the model's
  encrypted reasoning blob.
- `session_configured` is ~1.4 KB (skill descriptions, permission
  profile, rollout path).
- `task_started` adds ~200 bytes per turn.
- `mcp_startup_*` adds 200-400 bytes per external MCP child Codex
  itself spawns.

The trace contains >25 events for what becomes ~5 translated `agent.*`
frames. Steady-state, each codex frame with `raw` enabled is 2-5×
larger than without it.

### Current state

`raw_response_item` is dropped from the primary stream regardless of
`include_raw_events` — there's no `agent.*` equivalent. So in the
captured trace the worst-case multi-KB blobs don't reach translated
frames. **But** every event we *do* translate (`session_configured`,
`task_started`, `mcp_startup_*`, …) carries its full `raw` body when
opted in.

Today the code has the `slow_consumer` watchdog (good) but no
observability around byte volume.

### Failure modes

- **Durable log balloons.** No rotation, no cap, no compression. A
  talkative codex session at 4× per-frame size for hours = multi-MB
  per session per hour. Across many sessions a developer leaves
  running, disk usage climbs. The daemon has no health-check that
  surfaces this; user finds out via a disk-full alert.
- **Slow consumers stall faster.** The connection-level writer queue
  is 1024 frames bounded. Bigger frames shrink the back-pressure
  window proportionally — a flaky client trips `slow_consumer` sooner.
- **Ring buffer evicts useful frames faster.** Ring is count-bounded,
  not byte-bounded. Less concerning; mostly a documentation issue
  (should note the unit).

### Suggested fix

Three pieces, ranked by effort:

1. **Measure first.** A one-shot script that opens a real codex
   session with `include_raw_events:true`, runs N turns, and counts:
   bytes written to `<session>.jsonl`, average frame size on the
   wire, peak `raw` payload size for any single frame. Decide a cap
   based on the measurement. Back-of-envelope guess: ~5-10 KB/turn
   steady state, rare peaks > 50 KB if encrypted reasoning ever lands
   in `raw`.

2. **Cap individual `raw` payloads.** In `_raw_for(msg, meta)` in
   `translate_codex.py`, if `len(json.dumps(msg)) > MAX_RAW_BYTES`,
   drop the `raw` field for that frame and emit
   `agent.notice{category:"raw_truncated", data:{size, type}}` once
   per session. A 32 KB cap passes 99% of events while bounding the
   worst case.

3. **Cap or rotate the durable log.** Bigger lift; defer until #1
   tells us we need it. Two reasonable shapes:
   - Soft cap per file (e.g. 50 MB) with rotation:
     `<session>.jsonl` → `<session>.jsonl.1`, drop the oldest.
     Reload-on-reopen needs to read across rotations, which
     complicates the Phase 1 ring-seeding code.
   - Reject new writes once a file hits a hard cap (e.g. 200 MB) and
     surface as `agent.error{code:"log_full"}`. Less work, more
     user-visible.

### A cheap holding action

Even before #1 above, one small change bounds the worst case: **drop
`_meta` from the `raw` payload** in `_raw_for`. Today we copy `_meta`
(`requestId`, `threadId`) into `raw` for every frame. The translator
already extracts what it needs from `_meta` separately; storing it
again per-frame is duplicative and adds ~80 bytes × every frame ×
every codex session × forever. ~30-line change in `translate_codex.py`
plus a one-line schema doc tweak. Could land alongside Phase 6
cleanup.

### Why this hasn't bitten yet

`include_raw_events` is opt-in and the test suite uses it sparingly
(only `test_include_raw_events_carries_raw_payload`, one short turn).
The bench runs use the default (off). The first user to hit it will
be someone building a debug UI that surfaces native events to an
internal team — exactly the audience most likely to leave the option
on for hours per session.
