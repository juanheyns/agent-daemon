# `agent.*` event vocabulary

Authoritative mapping for the unified event namespace introduced in
`blemees/2`. Every event the daemon forwards is normalised into one of
the types below, regardless of which backend produced it.

This document is the contract for the translation layer. The two
backends speak very different native protocols:

* **Claude Code** writes [Anthropic stream-json](https://docs.anthropic.com/en/docs/build-with-claude/streaming) line-delimited
  events on the `claude -p` child's stdout: `system`, `stream_event`
  (Anthropic Messages API `MessageStreamEvent`), `assistant`, `user`,
  `partial_assistant`, `result`.
* **Codex** runs as an MCP server (`codex mcp-server`). It speaks
  JSON-RPC 2.0 over stdio, with a custom `notifications/codex/event`
  for streaming. The standard MCP `notifications/progress` is not
  used. Each event carries `_meta.{requestId,threadId}` and a
  `msg.{type,...}` body. The final result of a `tools/call` arrives as
  the JSON-RPC response, with `structuredContent.threadId` for resume.

The translation table below maps both feeds onto a single set of
`agent.*` frames clients can switch on without branching by backend.

## Common fields

Every `agent.*` frame the daemon emits carries:

| Field | Required | Notes |
|---|---|---|
| `type` | yes | One of the types in the table below. |
| `session_id` | yes | The blemees session id (the daemon's, not the backend's). |
| `seq` | yes | Monotonic per-session integer. |
| `backend` | yes | `"claude"` or `"codex"`. Lets clients still distinguish if they want to. |
| `raw` | optional | The native event the daemon translated from. Off by default; opt in per session via `blemeesd.open.options.<backend>.include_raw_events: true`. Format is the *un-namespaced* native frame (CC's stream-json line dict, or Codex's `msg` body). |

The on-the-wire shape never carries `null`s for absent fields — keys
either appear with a value or are omitted entirely.

## Event vocabulary

| Type | Purpose | Payload (besides common fields) |
|---|---|---|
| `agent.system_init` | First frame after spawn. Tells the client which backend, model, cwd, tools, and (for backends whose internal id differs from `session_id`) `native_session_id` are in play. The `native_session_id` field is present **only when it differs from `session_id`** — absent on Claude (CC's `--session-id` accepts our value verbatim), present on Codex (the `threadId`). | `model?, cwd?, tools?, native_session_id?, capabilities?, context_window?` |
| `agent.delta` | Incremental output during a turn. | `kind: "text" \| "thinking" \| "tool_input"`, plus one of: `text` (text/thinking) or `partial_json` (tool_input). May carry `item_id?` (Codex) or `index?` (CC content-block index) for clients that want to reassemble. |
| `agent.message` | A complete message from the assistant role (post-stream). | `role: "assistant", content: [...], phase?` |
| `agent.user_echo` | Echo of the user's input message. **Off by default on both backends** — opt in via `options.<backend>.user_echo: true`. (Claude additionally emits user-frame fan-outs for tool-result-bearing `user` events regardless of the toggle — see the translation table below.) | `message: { role:"user", content:... }` |
| `agent.tool_use` | A tool invocation request emitted by the model. | `tool_use_id, name, input` |
| `agent.tool_result` | The result the backend received for a tool invocation. | `tool_use_id, output, is_error?` |
| `agent.notice` | Backend-side informational events that are neither output nor errors — `mcp_startup_*` from codex, rate-limit pings, etc. Clients may ignore. | `level: "info" \| "warn", category: string, text?, data?` |
| `agent.result` | Turn-end. Always the last frame for a turn (including on crash, auth failure, or interrupt — the daemon synthesises one when the backend doesn't). The daemon uses this to mark `turn_active=False`. | `subtype: "success" \| "error" \| "interrupted" \| ..., duration_ms?, num_turns?, turn_id?, time_to_first_token_ms?, usage?: NormalisedUsage, error?` |

`NormalisedUsage`:

```jsonc
{
  "input_tokens": 0,
  "output_tokens": 0,
  "cache_read_input_tokens": 0,       // CC `cache_read_input_tokens` / Codex `cached_input_tokens`
  "cache_creation_input_tokens": 0,   // CC only; absent for Codex
  "reasoning_output_tokens": 0        // Codex only; absent for CC
}
```

The accumulator keeps unknown keys verbatim so future fields pass
through (existing CC behaviour). `reasoning_output_tokens` is **not**
folded into `output_tokens`: surfacing them separately matches what
Codex actually meters and lets clients budget independently.

## Symmetry guarantees

Beyond verbatim translation, the daemon also synthesises frames so the
two backends present a uniform turn lifecycle. Clients can rely on the
following invariants regardless of backend:

- **Every turn closes with `agent.result`.** If the backend never emits
  one (crash mid-turn, auth failure, hard interrupt), the daemon
  synthesises a closing `agent.result` with the appropriate `subtype`
  (`error` or `interrupted`) and an `error: {code, message}` block.
  Codes used in synth results: `backend_crashed`, `auth_failed`. The
  `blemeesd.error` frame is *also* emitted for visibility — clients
  that wait on `agent.result` to detect turn end will not hang.

- **Every turn opens with `agent.notice{category:"task_started"}`.**
  Codex emits this natively from `task_started`; the Claude backend
  synthesises one when it writes the user turn to stdin. Carries
  `data: {turn_id, started_at_ms}` on both backends. (Codex's wire
  field `started_at` is Unix seconds; the translator normalises to
  `started_at_ms` in milliseconds so the field name *and* unit
  match claude's synth notice — see the codex translation table
  below.) Clients can use it for "model is thinking…" UI hooks on
  both backends.

- **`agent.result.turn_id` is always set.** Codex carries it from
  `task_complete`; for Claude the daemon allocates a per-turn UUID hex
  in `send_user_turn` and stamps it on the eventual `agent.result`
  (including all synth variants). The same id appears in the
  preceding `task_started` notice so clients can correlate.

- **`agent.result.time_to_first_token_ms` is always set when the
  turn produced any model output.** Codex's value comes from
  `task_complete` (model-side measurement). The Claude value is
  daemon-side wall-clock from the `send_user_turn` write to the
  first model-output frame — `agent.delta` (long replies) or
  `agent.message` / `agent.tool_use` (short replies CC didn't chunk
  into deltas). The two measurements aren't directly comparable but
  both bound the same first-output latency envelope.

- **`agent.system_init.capabilities` is populated for both backends.**
  Codex pulls from `session_configured`. Claude synthesises from
  `options.claude.*`: `permission_mode` (verbatim),
  `reasoning_effort` (renamed from `effort` to match Codex's name),
  `rollout_path` (the `~/.claude/projects/<cwd>/<session>.jsonl`
  path).

- **`native_session_id` is present iff it differs from `session_id`.**
  On both `blemeesd.opened` and `agent.system_init` the field is
  emitted only when the backend's internal id differs from the
  daemon's. Absence is the canonical signal "use `session_id`
  directly". For Claude this means it's never present (CC's
  `--session-id` accepts our UUID verbatim). For Codex it shows up
  on resume (cached `threadId`) and on the `system_init` after the
  first turn's `session_configured`.

- **`agent.message.phase` carries the same semantic on both
  backends.** Codex emits `phase:"final_answer"` from
  `item_completed{AgentMessage}`. Claude derives the same value
  from CC's `assistant` event when the content has no `tool_use`
  blocks; mid-turn messages (those calling a tool) omit `phase`
  rather than mislabel them.

- **`agent.notice{rate_limits}.data` has a unified envelope.**
  Both translators emit `data.limit` (and optional
  `data.secondary_limit` for codex paid plans) carrying the
  cross-backend fields when known: `resets_at_ms` (Unix ms),
  `used_percent`, `window_minutes`, `status`. Vendor-specific
  extras (codex's `plan_type`/`limit_id`/…; CC's overage flags
  and event UUID) land under `data.vendor`. Cross-backend code can
  read `data.limit.resets_at_ms` or `data.limit.used_percent`
  without branching by backend; debug tooling can read
  `data.vendor` for full fidelity.

For the residual asymmetries the daemon does **not** try to paper
over — reasoning/thinking deltas, MCP startup chatter, the
codex-side tool-use coverage gap, and Claude's tool-result-block
splitting — see [`docs/asymmetries.md`](asymmetries.md).

## Translation: Claude Code → `agent.*`

| CC native event | `agent.*` translation | Notes |
|---|---|---|
| (synth, daemon-side, on `send_user_turn`) | `agent.notice{category:"task_started", data:{turn_id, started_at_ms}}` | Synthesised so Claude has a turn-start hook parallel to Codex's native `task_started`. `turn_id` is a per-turn UUID hex; reused on the closing `agent.result`. |
| `system{subtype:"init"}` | `agent.system_init{model, cwd, tools, capabilities: {permission_mode?, reasoning_effort?, rollout_path}}` | One frame per spawn. Pass `tools` array through verbatim. `capabilities` is daemon-synthesised from `options.claude.*` so the shape parallels Codex's. `native_session_id` is **deliberately omitted** for Claude — it would always equal `session_id`; absence is the wire-level "use session_id directly" signal. |
| `system{subtype:"<other>"}` | `agent.notice{category:"system_<subtype>", data:<rest>}` | Forward-compat for future CC system frames. |
| `stream_event{message_start}` | dropped (folded into `agent.system_init` if not yet emitted) | |
| `stream_event{content_block_start{type:"text"}}` | dropped | Block boundary; deltas alone carry the content. |
| `stream_event{content_block_start{type:"tool_use", id, name, input?}}` | `agent.tool_use{tool_use_id:id, name, input: {} \| input}` | Initial tool_use blocks usually have empty `input` filled in by `input_json_delta` events. |
| `stream_event{content_block_start{type:"thinking"}}` | dropped | |
| `stream_event{content_block_delta{delta:{type:"text_delta", text}}}` | `agent.delta{kind:"text", text, index}` | |
| `stream_event{content_block_delta{delta:{type:"thinking_delta", thinking}}}` | `agent.delta{kind:"thinking", text:thinking, index}` | |
| `stream_event{content_block_delta{delta:{type:"input_json_delta", partial_json}}}` | `agent.delta{kind:"tool_input", partial_json, index}` | Client must accumulate. |
| `stream_event{content_block_stop}` | dropped | |
| `stream_event{message_delta{usage}}` | dropped | Final usage arrives on `result`. |
| `stream_event{message_stop}` | dropped | |
| `assistant{message}` | `agent.message{role:"assistant", content: message.content, phase?:"final_answer"}` | `phase:"final_answer"` is daemon-derived to match codex's `AgentMessage.phase`: a message with no `tool_use` content blocks is the final answer to the user. Messages that *do* contain `tool_use` blocks are mid-turn (model is calling a tool) — `phase` is omitted there rather than mislabelled. |
| `partial_assistant{message}` | dropped (only `--include-partial-messages` produces these; redundant once we emit deltas) | |
| `user{message: {content: string \| [text-only]}}` | `agent.user_echo{message}` | Only when `options.claude.user_echo:true` (passes `--replay-user-messages` to CC). Off by default. |
| `user{message: {content: [..., {type:"tool_result", tool_use_id, content, is_error}, ...]}}` | one `agent.tool_result{tool_use_id, output:content, is_error}` per `tool_result` block; remaining text blocks emit a single `agent.user_echo`. | Tool-result fan-out happens regardless of `user_echo`; the surrounding text echo follows the same toggle as the row above. |
| `result{subtype, duration_ms, num_turns, usage}` | `agent.result{subtype, duration_ms, num_turns, turn_id, time_to_first_token_ms?, usage: <pass-through>}` | `turn_id` is the per-turn UUID hex; `time_to_first_token_ms` is daemon-side wall-clock from `send_user_turn` to the first model-output frame (`agent.delta` for long replies, `agent.message` / `agent.tool_use` for short replies CC didn't chunk). Both daemon-synthesised. |
| `rate_limit_event{rate_limit_info, …}` | `agent.notice{level:"info", category:"rate_limits", data: {limit, vendor}}` | Per-turn CC rate-limit ping. Same unified `data.limit` envelope codex uses. The translator extracts `rate_limit_info.resetsAt` (Unix seconds → `resets_at_ms`) and `status` into `data.limit`; everything else (`rateLimitType`, overage flags, event-level `uuid` / `session_id`) lands under `data.vendor`. |
| (synth, daemon-side; CC subprocess crashed mid-turn) | `agent.result{subtype:"error", num_turns:1, turn_id, time_to_first_token_ms?, error:{code:"backend_crashed", message}}` | Emitted alongside `blemeesd.error{backend_crashed}` so clients waiting on `agent.result` see a clean turn close (spec §5.6 invariant). |
| (synth, daemon-side; CC stderr matched auth-failure pattern mid-turn) | `agent.result{subtype:"error", num_turns:1, turn_id, time_to_first_token_ms?, error:{code:"auth_failed", message}}` | Emitted alongside `blemeesd.error{auth_failed}`. |

## Translation: Codex MCP → `agent.*`

Codex's stream is `notifications/codex/event` frames carrying
`msg.{type,...}`. Most fields below come from `msg`; `_meta.threadId`
is the native session id surfaced on `agent.system_init`. The final
`agent.result` is synthesised from the JSON-RPC `result` of the
originating `tools/call`, plus the preceding `task_complete` and last
`token_count`.

| Codex `msg.type` | `agent.*` translation | Notes |
|---|---|---|
| `session_configured` | `agent.system_init{model, cwd, native_session_id: msg.session_id, capabilities: {sandbox_policy, approval_policy, permission_profile, reasoning_effort, rollout_path}}` | One per spawn. `model_provider_id`, `history_*` go under `raw`. |
| `mcp_startup_update` | `agent.notice{level:"info", category:"backend_mcp_startup", data:{server, status}}` | Codex's own external MCP children. |
| `mcp_startup_complete` | `agent.notice{level:"info", category:"backend_mcp_startup_complete", data:{ready, failed, cancelled}}` | |
| `task_started` | `agent.notice{level:"info", category:"task_started", data:{turn_id, model_context_window, started_at_ms}}` *or* fold `model_context_window` into `agent.system_init` if not yet emitted | We chose: fold context window into init when known; emit notice with `turn_id`. Codex sends `started_at` in Unix **seconds**; the translator multiplies by 1000 and renames to `started_at_ms` so the unit + field name match claude's synth notice and the daemon's ms-everywhere convention (`last_turn_at_ms`, etc). |
| `raw_response_item` | dropped from primary stream; surfaced under `raw` when opt-in | Duplicates the structured `item_*` events. |
| `item_started{item:{type:"UserMessage", content}}` | dropped (we wait for completed) | |
| `item_completed{item:{type:"UserMessage", content}}` | `agent.user_echo{message:{role:"user", content: <translated>}}` | Only when `options.codex.user_echo:true`. Off by default — translator drops the frame. |
| `item_started{item:{type:"AgentMessage", id, content}}` | dropped (we emit deltas as they arrive) | |
| `item_completed{item:{type:"AgentMessage", id, content, phase}}` | `agent.message{role:"assistant", content: <translated>, phase}` | |
| `item_started{item:{type:"Reasoning", id}}` / `item_completed{item:{type:"Reasoning", ...}}` | dropped from primary stream | Encrypted reasoning is opaque; appears under `raw`. |
| `agent_message_content_delta{item_id, delta}` | `agent.delta{kind:"text", text:delta, item_id}` | |
| `agent_message_delta{delta}` | dropped (duplicate of `agent_message_content_delta`) | Both flavours arrive; we keep only the one with `item_id`. |
| `agent_message{message, phase}` | dropped (duplicate of `item_completed{AgentMessage}`) | |
| `user_message{message}` | dropped (duplicate of `item_completed{UserMessage}`) | |
| `token_count{info: null, rate_limits}` | `agent.notice{level:"info", category:"rate_limits", data: {limit, secondary_limit?, vendor}}` | Mid-turn rate-limit ping. The translator normalises the codex payload into the cross-backend shape: `data.limit.{resets_at_ms?, used_percent?, window_minutes?, status?}` (extracted from `rate_limits.primary` with `resets_at` converted from Unix seconds to ms); `data.secondary_limit` follows the same shape when `rate_limits.secondary` is non-null; `data.vendor` carries everything else verbatim (`plan_type`, `limit_id`, `rate_limit_reached_type`, `credits`, …). |
| `token_count{info:{total_token_usage, last_token_usage, model_context_window}, rate_limits}` | held; folded into the synthesised `agent.result.usage` (using `last_token_usage`) at turn end. | |
| `exec_command_begin` (and family) | `agent.tool_use{tool_use_id: msg.call_id, name:"shell" \| msg.tool, input: msg.command \| msg.params}` | *Not observed in the captured trace; mapping locked from Codex source. Re-trace with a tool-using prompt before Phase 3 implementation.* |
| `exec_command_end` (and family) | `agent.tool_result{tool_use_id, output, is_error}` | Same caveat. |
| `task_complete{turn_id, duration_ms, time_to_first_token_ms, last_agent_message}` | folded into the synthesised `agent.result` | |
| JSON-RPC `result{content, structuredContent:{threadId, content}}` | terminal `agent.result{subtype:"success", duration_ms, num_turns:1, turn_id, time_to_first_token_ms?, usage}` | Errors surface as `subtype:"error"` with the JSON-RPC error data on `agent.result.error`. |
| Cancelled turn (we sent `notifications/cancelled`) | `agent.result{subtype:"interrupted"}` | |
| (synth, daemon-side; codex subprocess crashed mid-turn) | `agent.result{subtype:"error", num_turns:1, turn_id?, time_to_first_token_ms?, error:{code:"backend_crashed", message}}` | Emitted alongside `blemeesd.error{backend_crashed}` so clients waiting on `agent.result` see a clean turn close. Mirrors the Claude path. |

## Inbound: user turns

Inbound from client to daemon stays a single shape regardless of
backend:

```jsonc
{
  "type": "agent.user",
  "session_id": "<blemees session>",
  "message": {
    "role": "user",
    "content": "..." // or [content blocks]
  }
}
```

Per-backend translation:

* **Claude:** the daemon writes one stream-json line on `claude -p`
  stdin: `{"type":"user","message":<message>,"session_id":<CC native id>}`. `content` may be a string or an array of CC content blocks (text, image, document, …); the daemon does not validate the inner block types.
* **Codex:** the daemon issues a `tools/call` with `name:"codex"` (first turn) or `name:"codex-reply"` (subsequent turns) and `arguments:{prompt:<string>, threadId:<native id>?}`. Multimodal `content` arrays are flattened to a single string by concatenating text blocks; non-text blocks are rejected with `invalid_message` until Codex grows the inputs.

A future addition (`agent.user.attachments` or similar) can lift this
limitation when Codex exposes image/file inputs through MCP. For
`blemees/2`, text-only is the documented behaviour for the codex backend.

## What `raw` carries

When the client opens a session with `options.<backend>.include_raw_events: true`,
every `agent.*` frame produced from a native event includes the original
event under `raw`:

* **Claude:** the un-prefixed CC stream-json dict (e.g.
  `{"type":"stream_event","event":{...}}`).
* **Codex:** the contents of `msg` from the `notifications/codex/event`
  notification (e.g. `{"type":"agent_message_content_delta","item_id":"...","delta":"..."}`),
  plus a sibling `_meta` field copied from the notification when present.

Synthetic frames (`agent.system_init` assembled from multiple events,
the synthesised `agent.result`) carry `raw` as `null` or omit it.

## Drift policy

This document is the source of truth for both the translation layer and
the schema set under `blemees/schemas/`. When a backend grows a new
event type:

1. Capture a fresh trace under `docs/traces/`.
2. Add a row to the relevant translation table above.
3. Decide: drop, route to existing `agent.*` type, or extend the vocab.
   Extending the vocab is a `blemees/3` change.
4. Update schemas + tests in lockstep.

Backends that gain *fields* on an existing event type are non-breaking:
the translator is permissive on input, and `additionalProperties: true`
on output payload schemas means clients see the new field automatically.
