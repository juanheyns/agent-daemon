# blemees — wire-frame JSON Schemas

Machine-readable contract for every frame on the `blemees/2` protocol.
The prose spec is the repository root `README.md`; the unified
`agent.*` event vocabulary is locked in
[`docs/agent-events.md`](../../docs/agent-events.md). The schemas in
this directory formalize the frame shapes referenced there.

These ship inside the `blemees` wheel as the `blemees.schemas`
subpackage, so installed clients can validate frames without copying
JSON anywhere:

```python
from blemees.schemas import load, iter_schemas, files

hello = load("inbound/blemeesd.hello.json")   # parsed dict
all_frames = list(iter_schemas())             # every shipped schema
root = files()                                # importlib.resources Traversable
```

## Layout

```
blemees/schemas/
  _common.json               # shared $defs (SessionId, Seq, Backend, AgentUserMessage, NormalisedUsage, …)
  inbound/                   # client → daemon frames
    blemeesd.hello.json
    blemeesd.open.json
    blemeesd.interrupt.json
    blemeesd.close.json
    blemeesd.list_sessions.json
    blemeesd.ping.json
    blemeesd.status.json
    blemeesd.watch.json
    blemeesd.unwatch.json
    blemeesd.session_info.json
    options.claude.json      # per-backend options consumed by blemeesd.open
    options.codex.json
    agent.user.json          # client user-turn (backend-neutral)
  outbound/                  # daemon → client frames
    blemeesd.hello_ack.json
    blemeesd.opened.json
    blemeesd.closed.json
    blemeesd.interrupted.json
    blemeesd.error.json
    blemeesd.stderr.json
    blemeesd.replay_gap.json
    blemeesd.sessions.json
    blemeesd.session_taken.json
    blemeesd.pong.json
    blemeesd.status_reply.json
    blemeesd.watching.json
    blemeesd.unwatched.json
    blemeesd.session_info_reply.json
    agent.event.json         # unified envelope for every translated agent.* event
```

## Draft / compatibility rules

* **Draft**: JSON Schema `2020-12` (via `$schema`).
* **Inbound frames** are strict (`additionalProperties: false`) — the
  daemon rejects unknown fields with `invalid_message`. The
  per-backend `options.<backend>` schemas inherit this strictness, so
  unknown knobs inside `options.claude` / `options.codex` are
  rejected too. Reserved unsafe fields under `options.claude` (legacy
  `dangerously_skip_permissions`, `--bare`, `--continue`, etc.) are
  refused via a `not` clause.
* **Outbound frames** permit `additionalProperties: true` so the daemon
  can grow the envelope (e.g. new debug fields) without breaking
  conforming clients.
* **`agent.*` events** use a single envelope schema
  (`schemas/outbound/agent.event.json`) with `if/then` branches per
  type. The eight types — `agent.system_init`, `agent.delta`,
  `agent.message`, `agent.user_echo`, `agent.tool_use`,
  `agent.tool_result`, `agent.notice`, `agent.result` — each carry
  the common envelope (`type`, `session_id`, `seq`, `backend`) plus
  type-specific required fields. Inner shapes (`content` arrays,
  tool `input`/`output`, `agent.notice.data`) stay permissive so
  future fields pass through.

## Use

Validate a frame with any JSON Schema 2020-12 library. From an
installed `blemees` wheel:

```python
from blemees.schemas import iter_schemas
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

# Build a registry once so $refs (e.g. into _common.json) resolve.
store = {s["$id"]: s for s in iter_schemas()}
registry = Registry()
for uri, schema in store.items():
    registry = registry.with_resource(uri, Resource.from_contents(schema))

def validate(frame_type: str, frame: dict, direction: str = "inbound") -> None:
    url = f"https://blemees/schemas/{direction}/{frame_type}.json"
    Draft202012Validator(store[url], registry=registry).validate(frame)
```

If you need on-disk paths (for tooling that does not understand
`importlib.resources`), use `as_file`:

```python
from importlib.resources import as_file
from blemees.schemas import files

with as_file(files() / "inbound" / "blemeesd.hello.json") as p:
    print(p)   # real filesystem path you can hand to a generator
```

Generators (`datamodel-code-generator`, `quicktype`, etc.) can turn
these schemas into typed models in most languages.

## Versioning

Breaking changes to any frame shape bump the protocol version
(`blemees/2` → `blemees/3`); the daemon rejects old versions on
`blemeesd.hello` with `code: protocol_mismatch`. Additive,
backward-compatible changes stay on the same version. The daemon
supports a single protocol version at a time — pre-1.0 has no
compatibility shim for `blemees/1`.
