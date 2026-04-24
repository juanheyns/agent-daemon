---
title: JSON Schemas
nav_order: 3
permalink: /schemas/
---

<!-- Auto-synced from schemas/README.md by .github/workflows/docs-sync.yml. -->

# blemees — wire-frame JSON Schemas

Machine-readable contract for every frame on the `blemees/1` protocol.
The prose spec is the repository root `README.md`; the schemas in this directory
formalize the frame shapes referenced there.

## Layout

```
schemas/
  _common.json               # shared $defs (SessionId, Seq, MessageContent, …)
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
    claude.user.json
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
    claude.event.json        # envelope for every forwarded CC event
```

## Draft / compatibility rules

* **Draft**: JSON Schema `2020-12` (via `$schema`).
* **Inbound frames** are strict (`additionalProperties: false`) — the
  daemon rejects unknown fields with `invalid_message`. Fields the
  daemon owns (`input_format`, `output_format`) and the legacy unsafe
  flags (`dangerously_skip_permissions`, …) are refused explicitly via
  a `not` clause on `blemeesd.open`.
* **Outbound frames** permit `additionalProperties: true` so the daemon
  can grow the envelope (e.g. new debug fields) without breaking
  conforming clients.
* **`claude.*` events** use a loose envelope
  (`schemas/outbound/claude.event.json`) — only `type`, `session_id`, and
  `seq` are constrained; the inner CC payload (`message`, `event`,
  `result`, …) is not validated here, because Claude Code owns that
  schema and we are pass-through.

## Use

Validate a frame with any JSON Schema 2020-12 library. In Python:

```python
import json
from pathlib import Path
from jsonschema import Draft202012Validator, RefResolver

schemas_dir = Path("schemas").resolve()
store = {
    # Load every schema into an $id → schema map so $refs resolve locally.
    json.loads(p.read_text())["$id"]: json.loads(p.read_text())
    for p in schemas_dir.rglob("*.json")
}

def validate(frame_type: str, frame: dict, direction: str = "inbound") -> None:
    url = f"https://blemees/schemas/{direction}/{frame_type}.json"
    schema = store[url]
    resolver = RefResolver(base_uri=url, referrer=schema, store=store)
    Draft202012Validator(schema, resolver=resolver).validate(frame)
```

Generators (`datamodel-code-generator`, `quicktype`, etc.) can turn
these schemas into typed models in most languages.

## Versioning

Breaking changes to any frame shape bump the protocol version (`blemees/1`
→ `blemees/2`); the daemon rejects old versions on `blemeesd.hello`
with `code: protocol_mismatch`. Additive, backward-compatible changes
stay on the same version.
