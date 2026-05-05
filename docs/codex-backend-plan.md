# Codex backend plan

Status of the multi-phase work to add `codex mcp-server` as a second
`AgentBackend` alongside `claude -p`, with the unified `agent.*` wire
namespace.

The unified event vocabulary itself is locked in
[`docs/agent-events.md`](agent-events.md); the captured `codex
mcp-server` traces that grounded it live under [`docs/traces/`](traces/).

## Status at a glance

| Phase | Title | Status |
|---|---|---|
| 0 | Spec & vocabulary lock | ✅ done |
| 1 | Backend abstraction | ✅ done (combined with Phase 2) |
| 2 | `agent.*` translator for Claude backend | ✅ done |
| 3 | Codex backend MVP | ✅ done |
| 4 | Codex resume / interrupt / list | ✅ done |
| 5 | Hardening | ✅ done |
| 6 | Cleanup | ✅ done |

The daemon advertises `blemees-agent/1`, emits `agent.*` end-to-end on both
backends, and supports the full open / user / interrupt / close /
list_sessions / resume cycle for `backend:"codex"`. Phase 5 hardening
landed: `requires_codex` pytest mark + `tests/blemees/test_daemon_e2e_codex.py`,
`blemees/bench.py` rewritten with `--backend {claude,codex}`, structured
JSON-RPC auth-error classification (`error.data.code` /
`error.data.type` / message-pattern), mixed-backend
`agent.status_reply.sessions.by_backend` test, `BLEMEES_AGENTD_CODEX`
documented in both the systemd unit and the launchd plist, the
existing `requires_claude` e2e suite migrated off the legacy `blemees/1`
open shape, and the Codex `turn_aborted` event wired into
`agent.result{interrupted}` (so cancelled turns terminate cleanly even
when codex 0.125.x doesn't follow up with a JSON-RPC reply). Late
events tagged with the aborted turn's `requestId` are filtered so
they don't leak into the next turn's frame stream. Mock test suite:
265 passed, 6 skipped. All 6 e2e tests (3 claude + 3 codex) pass
against the real CLIs. Bench numbers captured in
[`docs/codex-bench-baseline.md`](codex-bench-baseline.md).

---

## Phase 3 — Codex backend MVP

Goal: an `agent:"codex"` open succeeds, runs a turn, streams
`agent.delta`, and returns `agent.result`. Single-turn only — resume,
on-disk discovery, and interrupt arrive in Phase 4.

**New code:**

- `blemees/backends/codex.py` — `CodexBackend` implementing the
  `AgentBackend` Protocol from `blemees/backends/__init__.py`:
  - Spawn `codex mcp-server` (`-c key=value` synthesised from
    `options.codex.config`; `--enable`/`--disable` from
    `options.codex.config.features.<name>`).
  - JSON-RPC 2.0 client over stdio. Per-request `id` allocation; one
    pending future per outstanding `tools/call`.
  - `initialize` handshake on spawn (`protocolVersion:
    "2024-11-05"`); `notifications/initialized`; one `tools/list` to
    confirm `codex` and `codex-reply` are present.
  - `send_user_turn(message)` — flatten any text-block array to a
    single prompt string (reject non-text blocks with
    `invalid_message`); first turn → `tools/call{name:"codex"}`,
    subsequent turns → `tools/call{name:"codex-reply", threadId:
    cached}`.
  - Demux: `notifications/codex/event` → translator;
    `result`/`error` for the in-flight request id → synthesise
    terminal `agent.result`.

- `blemees/backends/translate_codex.py` — pure translator from the
  `msg.{type,...}` body of `notifications/codex/event` to `agent.*`
  per [`docs/agent-events.md`](agent-events.md). Mappings already
  decided on paper:
  - `session_configured` → `agent.system_init` (carry
    `model`, `cwd`, `native_session_id`, `capabilities`,
    `rollout_path`).
  - `task_started` → `agent.notice{category:"task_started"}`,
    folding `model_context_window` into a deferred
    `agent.system_init` field if not yet emitted.
  - `mcp_startup_*` → `agent.notice`.
  - `agent_message_content_delta` → `agent.delta{kind:"text"}` (drop
    the duplicate `agent_message_delta`).
  - `item_completed{AgentMessage}` → `agent.message`.
  - `item_completed{UserMessage}` → `agent.user_echo`; drop
    `user_message` and `item_started` duplicates.
  - `token_count` (mid-turn, `info: null`) →
    `agent.notice{category:"rate_limits"}`.
  - `token_count` (final, `info` populated) → buffered; folded into
    the synthesised `agent.result.usage` (with
    `cached_input_tokens` → `cache_read_input_tokens` rename and
    `reasoning_output_tokens` surfaced as a first-class field).
  - `task_complete` → buffered for the synthesised `agent.result`
    (`duration_ms`, `turn_id`, `time_to_first_token_ms`).
  - `raw_response_item`, `Reasoning` items → dropped from the primary
    stream; appear under `raw` when `include_raw_events` is set.
  - `exec_command_*` → `agent.tool_use` / `agent.tool_result`.
    *Mapping is pencilled in but not trace-verified — re-capture
    a tool-using prompt before declaring this row stable.*

**Daemon wiring:**

- `blemees/daemon.py:_make_backend` — replace the `claude`-only
  branch with a real Codex case that constructs `CodexBackend`. Drop
  the `raise UnknownBackendError(msg.backend)` fallback for codex.
- `blemees/backends/codex.py:detect_version` — parse
  `codex --version` output (`codex-cli 0.125.0` → `0.125.0`); already
  partly handled by `daemon.detect_codex_version`, fold in here.

**Tests:**

- `tests/blemees/fake_codex.py` — mock binary mirroring
  `fake_claude.py`. Reads scripted JSON-RPC requests, emits scripted
  `notifications/codex/event` + final response. Modes:
  - `normal` — single-turn happy path (matches the
    `docs/traces/codex-mcp-turn-*.jsonl` capture).
  - `crash` — emit a partial event, exit non-zero mid-turn.
  - `auth` — JSON-RPC error with auth-related code.
  - `slow` — keep emitting deltas until cancelled.
  - `tool` — emit `exec_command_*` events to drive the tool-use
    translation (once we re-capture a real trace).

- `tests/blemees/test_translate_codex.py` — pure translator tests
  using fixture frames from the real trace. One test per row of the
  translator mapping table.

- `tests/blemees/test_backend_codex.py` — backend-level tests
  against `fake_codex.py`. Covers handshake, single turn, deltas,
  usage normalisation (especially the
  `cached_input_tokens`→`cache_read_input_tokens` rename and
  `reasoning_output_tokens` survival), malformed JSON-RPC handling,
  rejection of non-text content blocks.

- `tests/blemees/test_daemon_mock.py` — extend with at least one
  end-to-end codex-backend turn through the daemon (using
  `fake_codex.py` as the binary).

**Exit criteria:** `uv run pytest tests/blemees/` stays green with
the new tests included; manual smoke against a real `codex mcp-server`
runs one turn through the daemon and produces `agent.*` frames
indistinguishable in shape from the Claude backend's.

---

## Phase 4 — Codex resume / interrupt / list

**Resume:**

- `Session` gains a `native_session_id: str | None` slot. Codex sets
  it from the first `session_configured` event; subsequent
  `tools/call` invocations route through `codex-reply` with the
  cached `threadId`.
- The `Session.open_msg.resume` flag becomes a backend hint —
  `ClaudeBackend` mutates argv (existing behaviour);
  `CodexBackend` re-uses the stored id.
- `agent.opened.native_session_id` is populated for both backends
  (currently always equals `session_id`).

**Interrupt:**

- `CodexBackend.interrupt()` sends
  `notifications/cancelled{requestId, reason:"user_interrupt"}`.
  Does **not** kill the child. Returns `True` if a turn was
  in-flight, mirroring the Claude contract.
- The interrupted turn produces `agent.result{subtype:"interrupted"}`.

**On-disk discovery:**

- `blemees/backends/codex.py:list_on_disk_sessions(cwd)` — walk
  `~/.codex/sessions/YYYY/MM/DD/rollout-*-<threadId>.jsonl`,
  filtering by the embedded `cwd` (Codex stores `cwd` inside the
  rollout's `session_configured` event; we extract it). Newest-first.
- `Session` caches the rollout path from `session_configured`;
  `close{delete:true}` unlinks it.
- `blemees/session.py:_backend_session_file_path` learns the codex
  case (currently returns `None`).
- `blemees/daemon.py:_handle_list_sessions` — currently
  claude-only; merge with codex on-disk listings, tagging each row
  with its `backend`.

**Tests:**

- `test_backend_codex.py` gains coverage for the
  resume / cancel / on-disk-list paths, mirroring the existing
  `test_backend_claude.py` cases.
- `test_daemon_mock.py` — list_sessions returns mixed-backend rows.

---

## Phase 5 — Hardening

**E2E tests:**

- New pytest mark `requires_codex` (parallel to `requires_claude`).
  Skip unless `codex` is installed and `codex login status` reports
  logged in.
- `tests/blemees/test_daemon_e2e_codex.py` (the README already
  promises this filename — see the §6 file-layout snippet) covering
  the same scenarios as the claude e2e suite: turn → text response,
  context across two turns, close → reattach, interrupt
  mid-generation.
- Verify the existing `requires_claude` e2e suite still passes
  against a real `claude` install — not exercised in Phase 1+2.

**Bench:**

- `blemees/bench.py` is currently untouched and likely broken by the
  protocol bump. Restore it with a `--backend {claude,codex}` flag.
- Document the codex-side acceptance targets from spec §11.4 (warm
  user → first delta ≤ 1.0 s; initialize-handshake cold-open cost
  recorded but not gated).

**Auth-error classification for codex:**

- `CodexBackend` learns the JSON-RPC error codes Codex returns for
  authn/authz failures (rather than the stderr-regex approach used
  for Claude). Surface as `auth_failed` with a backend-specific
  remediation message (`run \`codex login\``).

**Status snapshot:**

- Already populated `sessions.by_backend`; verify under load that
  the per-backend counters are accurate when both backends have
  active sessions.

**Packaging:**

- `packaging/blemees-agentd/blemees-agentd.service` — add a comment
  documenting `BLEMEES_AGENTD_CODEX` for users with non-PATH installs.
- `packaging/blemees-agentd/com.blemees.blemees-agentd.plist` — same.

---

## Phase 6 — Cleanup

Landed:

- `pyproject.toml` description reworded for backend neutrality
  (`"…exposing local agent CLIs (Claude Code, Codex) over a Unix
  socket."`); `keywords` extended with `codex`/`agent`.
- `cc-stdout-` / `cc-stderr-` / `cc-exit-` asyncio task names in
  `blemees/backends/claude.py` renamed to `claude-stdout-…` for
  symmetry with `codex-stdout-…`.
- Stale `claude.user` / `claude.event` references in
  `blemees/backends/translate_claude.py` and
  `blemees/schemas/inbound/agent.watch.json` swapped for the
  `agent.*` vocabulary.
- `tests/blemees/test_daemon_e2e.py` renamed to
  `test_daemon_e2e_claude.py` (via `git mv`) so the layout matches
  the README/spec snippet and pairs with the existing
  `test_daemon_e2e_codex.py`. The `_open_<backend>` helper pattern
  is preserved.
- README §6.2.4 (Codex interrupt) gained a paragraph on `turn_aborted`
  semantics and late-`requestId` filtering. New §6.2.6 documents the
  cross-process resume caveat on Codex 0.125.x. `docs/spec.md` and
  `docs/schemas.md` re-synced from sources.
- Verified: `blemees/schemas/README.md`'s "Reserved unsafe fields"
  wording matches what `options.claude.json`'s `not.anyOf` clause and
  `validate_options` enforce. No stale claude-specific imports left
  in `blemees/protocol.py`.

Mock test suite remains at 265 passed, 6 skipped after Phase 6.

### Context Phase 6 needs to know about (emerged in Phases 3–5)

These are *behavioural* facts about the codex backend that affect
how the README/spec wording should describe the system. None of them
are bugs to fix in Phase 6 — they're constraints the documentation
needs to reflect honestly.

- **Codex cross-process resume is broken in 0.125.x.**
  `tools/call codex-reply` does *not* reliably rehydrate state when
  called from a fresh `codex mcp-server` process with a prior
  `threadId`; it returns an empty success result. The daemon
  correctly issues `codex-reply` with the cached threadId on
  reattach (verified in `test_codex_session_resume_uses_codex_reply`)
  — codex itself is the problem. `blemees/bench.py` skips the
  `resume_first_event` step for codex; the e2e suite uses
  `test_real_codex_context_across_two_turns` (single-process,
  single-connection) instead of testing detach/reattach context.
  README/spec should be honest about this: claude resume preserves
  context across reattach; codex resume re-issues `codex-reply`
  correctly but model-side state is unstable on 0.125.x.
- **`turn_aborted` is the codex cancellation primitive.** When the
  daemon sends `notifications/cancelled`, codex 0.125.x emits
  `codex/event{type:"turn_aborted"}` and frequently does *not*
  follow up with a JSON-RPC reply. The daemon finalises the in-flight
  turn from the `turn_aborted` event itself (`translate_codex.py`
  buffers `turn_id`/`reason`; `CodexBackend._handle_notification`
  triggers `finalize_interrupted` and clears the active-turn slot).
  Don't describe codex interrupt as "wait for the response"; the
  semantics are "wait for `turn_aborted` *or* the response,
  whichever lands first."
- **Late-`requestId` events are filtered.** After `turn_aborted`
  codex keeps streaming events tagged with the cancelled turn's
  `requestId` — sometimes interleaved with events for the *next*
  turn. The daemon drops events whose `_meta.requestId` doesn't
  match the active turn (`CodexBackend._handle_notification`); this
  is what stops cross-turn pollution. Worth mentioning in the spec
  alongside the interrupt semantics.
- **Bench numbers are recorded, not gated.** `docs/codex-bench-baseline.md`
  has actual cold/warm/resume numbers per backend. Spec §11.4 lists
  *targets*, those are aspirational. README should link the baseline
  doc when describing performance characteristics.
- **`_FIRST_EVENT_TYPES` in `bench.py` deliberately excludes
  `agent.system_init` and `agent.notice`.** Earlier versions
  included them, which made "first event" latency ~3 ms (matching
  daemon-side init frames, not model output). If anyone "fixes" the
  bench by re-adding init/notice frames, they'll silently revert
  the timer to garbage.
- **Legacy claude e2e tests had been silently skipping.** Phase 5
  found that `tests/blemees/test_daemon_e2e.py` was sending
  `agent.open` with the pre-`blemees-agent/1` flat shape (top-level
  `model` / `tools` / `permission_mode`). The marker meant the
  failure stayed silent for two protocol bumps. Now migrated to
  `backend:"claude"` + `options.claude.{}`; if Phase 6 splits the
  file, preserve that shape.

### Follow-ups deferred past Phase 6

The three open questions in the next section have a deeper write-up
in [`docs/codex-followups.md`](codex-followups.md) — concrete
suggested fixes, how to verify, and why each hasn't bitten yet.
Phase 6 should *not* try to land those; they're real engineering
work that wants its own scope.

---

## Minor loose ends carried over from Phase 1+2

These didn't block Phase 1+2 but should land before tagging a
release:

- ~~`blemees/bench.py` — broken; deferred to Phase 5.~~ ✅ rewritten
  in Phase 5 with `--backend {claude,codex}`. See
  `docs/codex-bench-baseline.md` for numbers.
- ~~`tests/blemees/test_daemon_e2e.py` — single file, not split per
  backend (Phase 6 reconciliation).~~ ✅ renamed to
  `test_daemon_e2e_claude.py` in Phase 6.
- ~~`pyproject.toml` description — still claude-centric (Phase 6).~~
  ✅ reworded in Phase 6.

## Risks & open questions

Each item below has a deeper write-up in
[`docs/codex-followups.md`](codex-followups.md) — full context, code
pointers, suggested fix, and how to verify. The summaries here are
the single-paragraph version for quick scanning.

1. **Codex tool-call event shape drift.** The `exec_command_*` events
   are mapped on paper but not trace-verified. Re-capture with a
   tool-using prompt (`scripts/codex_trace.py --phase turn` with a
   prompt that triggers shell exec) before locking the translation
   in tests.
2. **Codex rollout `cwd` field.** The discovery walker filters on
   `cwd` extracted from each rollout's `session_configured` event.
   Confirm Codex always emits `cwd` there (it does in the captured
   trace, but the field is documented as optional).
3. **`raw` payload size for codex.** Codex's events are chattier
   than CC's (in particular the `raw_response_item` envelopes when
   `include_raw_events` is on). Measure ring-buffer growth
   empirically and consider a size cap if the durable log balloons.
4. **JSON-RPC framing.** Phase 0 confirmed Codex uses NDJSON over
   stdio (no LSP `Content-Length:` headers). If a future Codex
   release switches framing, the backend driver needs a header
   detector — flag in `agent.notice` and fail fast rather than silent
   drift.
