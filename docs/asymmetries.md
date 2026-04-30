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

## 5. CC `rate_limit_event` is unmapped

**Codex:** emits `token_count` events with rate-limit data; the
translator surfaces them as
`agent.notice{category:"rate_limits", data: <…>}`. Mapped row in
[`agent-events.md`](agent-events.md).

**Claude:** emits a `rate_limit_event` (top-level CC stream-json
event), but the translator has no row for it — they fall through to
the unknown-event path and surface as
`agent.notice{category:"claude_unknown_rate_limit_event"}`. Spotted
by [`scripts/transcript_compare.py`](../scripts/transcript_compare.py)
on a routine "pong" prompt — every Claude transcript currently carries
this stray notice.

**Why we left it alone:** unlike the items above this one is just
unfinished, not deferred. Should be a small change to
`translate_claude.py` to add a row mapping `rate_limit_event` → 
`agent.notice{category:"rate_limits", data: <event payload>}` so both
backends use the same category name.

**Future option:** capture a real `rate_limit_event` from CC (one
shows up after every turn, so any trace will do — see
`docs/traces/transcript-claude.txt`), lock the field shape, add the
translator row + a fixture-driven test. Estimated 30 min.

---

## 6. Frame ordering around `task_started` differs

**Codex:** native order is `session_configured` → `task_started` →
content events. The translator emits them in that order: `agent.system_init`
arrives first, then `agent.notice{task_started}`.

**Claude:** the daemon synthesises `agent.notice{task_started}` from
`send_user_turn` *before* CC's stdin is even read, so the notice
lands before `agent.system_init`. Strictly speaking this also leaks
the daemon's view of "turn started" earlier than the model's view.

**Why we left it alone:** swapping the order would require buffering
the synth notice until `agent.system_init` arrives — plumbing for
plumbing's sake unless a client actually trips on it. The frames are
seq-tagged so any client driven by `seq` order rather than wall clock
will handle either ordering fine.

**Future option:** if symmetry of frame *order* (not just frame set)
becomes important, gate the synth `task_started` emission on having
already emitted `agent.system_init` for this spawn — easy because
`ClaudeBackend` already knows the spawn lifecycle.

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
