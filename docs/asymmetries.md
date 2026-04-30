# Backend asymmetries the daemon does not paper over

`blemees/2`'s premise is that Claude Code and Codex sessions look the
same on the wire — clients switch on `agent.*` types without branching
by backend. The daemon goes further than verbatim translation in some
places: it synthesises closing `agent.result` frames on crashes and
auth failures, allocates `turn_id` for Claude, measures
`time_to_first_token_ms` daemon-side, populates `agent.system_init.capabilities`
for Claude, and emits a synthetic `agent.notice{category:"task_started"}`
for Claude. Those are documented in [`agent-events.md`](agent-events.md).

This doc catalogues the asymmetries that *remain* — places where the
two backends genuinely differ and the daemon has chosen not to bridge
the gap (either because it can't, or because doing so would produce
fake-looking data that obscures the real platform difference).

These are open design questions. Each item lists the asymmetry, why
it exists, why we left it alone, and the option(s) we'd consider if a
client need ever materialises.

---

## 1. Reasoning / thinking deltas

**Claude:** emits `agent.delta{kind:"thinking"}` from CC's
`thinking_delta` — the model's internal reasoning text streams
through verbatim.

**Codex:** drops `item_started{Reasoning}` and `item_completed{Reasoning}`
from the primary stream entirely. Codex's reasoning is
end-to-end-encrypted on the wire (the `EncryptedContent` block in
the rollout); the daemon never sees plain text it could surface as
`agent.delta{kind:"thinking"}`.

**Why we left it alone:** synthesising fake reasoning text for codex
would be a lie. The encrypted blob is opaque by design; even Codex's
own UI doesn't render it.

**Future option:** if a client needs a "model is thinking…" UI hook
on both backends, we could emit symmetric pseudo-events — e.g.
`agent.notice{category:"reasoning_started"}` /
`{category:"reasoning_completed"}` from both translators when the
model enters/exits a reasoning span. No fake text content; just a
boundary marker. Decision deferred until a client surfaces the need.

---

## 2. MCP startup chatter

**Codex:** emits `mcp_startup_update` and `mcp_startup_complete`
events as it boots its own external MCP children. The daemon
translates them into `agent.notice{category:"backend_mcp_startup",…}`.

**Claude:** also runs MCP children when configured (`--mcp-config`),
but its child management is invisible on the `claude -p` stdio. The
daemon has no native event to translate.

**Why we left it alone:** we can't synthesise events for state we
can't observe. CC may grow MCP-startup events on stream-json in a
future version, at which point the translator can pick them up.

**Future option:** if we cared enough we could parse stderr lines or
tail the MCP child's own logs, but both are fragile. Wait for CC to
expose this on stream-json.

---

## 3. Tool-use coverage gap on Codex

**Claude:** every tool call the model makes — bash, file
operations, MCP-served tools — produces `agent.tool_use` /
`agent.tool_result` pairs because they all flow through CC's
`stream_event{content_block_start{type:"tool_use"}}` /
`user{tool_result}` shapes.

**Codex:** the translator only handles `exec_command_*` (shell). MCP
tool calls and Codex's other tool surfaces fall through to the
generic `agent.notice{category:"codex_unknown_<type>"}` path.

**Why we left it alone:** this is a known issue tracked in
[`codex-followups.md` item 1](codex-followups.md#1-codex-tool-call-event-shape-exec_command_-is-not-trace-verified)
— even the existing `exec_command_*` mapping is fabricated from
Codex's source code rather than verified against a captured trace.
Closing this gap is a real workstream (capture traces, lock down
the wire shape, extend the translator), not something we'd patch
over with synthesis.

**Future option:** complete the codex-followups #1 plan. Once the
trace is captured and the mapping verified, this asymmetry largely
disappears.

---

## 4. `agent.user_echo` for tool-result blocks (Claude only)

**Claude:** `user{message: {content: [..., {type:"tool_result", …}, ...]}}`
events fan out into one `agent.tool_result` per `tool_result` block,
plus an `agent.user_echo` containing whatever leftover text blocks
remained. Real user turns also produce `agent.user_echo`.

**Codex:** `item_completed{UserMessage}` produces a single
`agent.user_echo`. Codex doesn't have the "user message that's
actually a list of tool results" pattern — tool results are emitted
as `exec_command_end` events (which become `agent.tool_result`).

**Why we left it alone:** the shapes are different because the
underlying mental models are different. CC's "tool results live
inside a synthetic user turn" is an Anthropic Messages API
convention; Codex's "tool results are first-class events" is an MCP
convention. Forcing one into the other would lose information either
way.

**Future option:** none worth pursuing. Document the difference;
clients that consume `agent.user_echo` for analytics can already
filter by whether the same turn produced `agent.tool_result` frames.

---

## 5. CC `rate_limit_event` is unmapped — *fixed*

**Status:** resolved. `translate_claude._translate_rate_limit_event`
maps CC's `rate_limit_event` → `agent.notice{level:"info",
category:"rate_limits", data:<rest>}`, the same shape codex emits
from `token_count{info:null}`. Pinned by
`tests/blemees/test_backend_claude.py::test_rate_limit_event_*`.

The translator passes every non-`type` field through under `data`,
so future CC additions propagate without a code change. Section
preserved here as a worked example of the drift policy below.

---

## 6. Frame ordering around `task_started` differs — *fixed*

**Status:** resolved. `ClaudeBackend.send_user_turn` now stashes the
synth `agent.notice{task_started}` and `_read_stdout` flushes it
immediately after forwarding `agent.system_init`. Frame order on
Claude now matches codex's native `session_configured → task_started
→ content events` flow. Pinned by
`test_task_started_notice_is_emitted_after_system_init`.

The `_system_init_emitted` / `_pending_task_started` state is
spawn-scoped — both reset on every `spawn()` call so a `--resume`
respawn re-defers until the new child's first init lands. On turn 2
and beyond (where init has already been emitted for the spawn) the
notice goes out immediately without buffering.

---

## 7. Short Claude turns produce no `agent.delta`

**Claude:** `--include-partial-messages` is off by default, and CC
only emits `stream_event{content_block_delta}` when the assistant
turn is long enough to chunk. Short replies (e.g. "pong") arrive as
a single `assistant{message}` with no preceding deltas at all.

**Codex:** every assistant turn produces at least one
`agent_message_content_delta` regardless of length, so
`agent.delta{kind:"text"}` always appears on Codex.

**Side effect:** Claude's `time_to_first_token_ms` (daemon-measured)
is *omitted* on these short turns because we never see a first delta
to clock against. The redacted transcripts show `agent.result`
without that field on Claude but with it on Codex.

**Why we left it alone:** this is upstream behaviour. Setting
`include_partial_messages: true` on the open options would force
deltas to appear, but that's a client choice — forcing it daemon-side
would change other shape (we'd start surfacing `partial_assistant`
events the translator currently drops).

**Future option:** if the missing TTFT bothers a client, we could
fall back to measuring TTFT from the `assistant` event (first complete
message). It's a coarser metric — "time to whole reply" vs "time to
first token" — but at least the field is always populated.

---

## Drift policy

When you find a new asymmetry that looks worth bridging, the
checklist is:

1. **Can the daemon observe it?** If we can't see the data on
   either side's stdio, we can't synthesise. Stop here.
2. **Does the synthesis look real, or fake?** Allocating a `turn_id`
   is fine — it's an identifier, no semantic content. Fabricating
   reasoning text would be misleading. Prefer notices and
   identifiers over content fabrication.
3. **Is there a concrete client need?** Symmetry for its own sake
   adds noise without payoff. Wait for a client to ask.
4. **Add to [`agent-events.md`](agent-events.md), schemas,
   tests, and this doc** in lockstep when the answer is yes.
