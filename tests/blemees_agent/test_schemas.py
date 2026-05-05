"""Validate the shipped JSON Schemas: parse, meta-validate, and accept
canonical example frames. Loads via `importlib.resources` so the test
exercises the same lookup path clients will use after `pip install`.
If ``jsonschema`` is not installed the test is skipped rather than
failed — schemas remain a contract even if the dev dep isn't available.
"""

from __future__ import annotations

import json
from importlib import resources

import pytest

jsonschema = pytest.importorskip("jsonschema")
from jsonschema import Draft202012Validator  # noqa: E402  (gated by importorskip)
from referencing import Registry, Resource  # noqa: E402

SCHEMAS_ROOT = resources.files("blemees_agent.schemas")


def _iter_schema_paths():
    for direction in ("inbound", "outbound"):
        for entry in (SCHEMAS_ROOT / direction).iterdir():
            if entry.name.endswith(".json"):
                yield entry
    common = SCHEMAS_ROOT / "_common.json"
    if common.is_file():
        yield common


def _load_all() -> dict[str, dict]:
    return {
        json.loads(p.read_text(encoding="utf-8"))["$id"]: json.loads(p.read_text(encoding="utf-8"))
        for p in _iter_schema_paths()
    }


def _registry(store: dict[str, dict]) -> Registry:
    reg = Registry()
    for uri, schema in store.items():
        reg = reg.with_resource(uri, Resource.from_contents(schema))
    return reg


# ---------------------------------------------------------------------------
# Public helpers (`blemees_agent.schemas.load`, `iter_schemas`, `files`).
# These exist precisely so end users don't need to know about
# importlib.resources; the tests here pin the contract.
# ---------------------------------------------------------------------------


def test_blemees_schemas_load_returns_parsed_schema():
    from blemees_agent.schemas import load

    s = load("inbound/agent.hello.json")
    assert s["$id"] == "https://blemees/schemas/inbound/agent.hello.json"
    assert s["$schema"] == "https://json-schema.org/draft/2020-12/schema"


def test_blemees_schemas_iter_yields_every_inbound_and_outbound_frame():
    from blemees_agent.schemas import iter_schemas

    ids = {s["$id"] for s in iter_schemas()}
    # Spot-check a few well-known frames; the exhaustive list is in
    # blemees/schemas/README.md.
    assert "https://blemees/schemas/inbound/agent.hello.json" in ids
    assert "https://blemees/schemas/inbound/agent.user.json" in ids
    assert "https://blemees/schemas/inbound/options.claude.json" in ids
    assert "https://blemees/schemas/inbound/options.codex.json" in ids
    assert "https://blemees/schemas/outbound/agent.hello_ack.json" in ids
    assert "https://blemees/schemas/outbound/agent.event.json" in ids


def test_blemees_schemas_files_is_traversable_and_lists_top_level():
    from blemees_agent.schemas import files

    names = {entry.name for entry in files().iterdir()}
    # Subpackages (inbound, outbound), the shared $defs file, and the README.
    assert {"inbound", "outbound", "_common.json", "README.md"}.issubset(names)


# ---------------------------------------------------------------------------
# Meta-validation
# ---------------------------------------------------------------------------


def test_every_schema_parses_and_declares_id_and_schema():
    for path in _iter_schema_paths():
        obj = json.loads(path.read_text())
        assert obj.get("$schema") == "https://json-schema.org/draft/2020-12/schema", path
        assert obj.get("$id", "").startswith("https://blemees/schemas/"), path


def test_every_schema_is_valid_against_draft_2020_12_metaschema():
    for path in _iter_schema_paths():
        obj = json.loads(path.read_text())
        # Raises SchemaError if the schema itself is malformed.
        Draft202012Validator.check_schema(obj)


# ---------------------------------------------------------------------------
# Example-frame validation (inbound + outbound).
# ---------------------------------------------------------------------------


def _validate(store, registry, schema_id: str, frame: dict) -> None:
    schema = store[schema_id]
    Draft202012Validator(schema, registry=registry).validate(frame)


def _assert_invalid(store, registry, schema_id: str, frame: dict) -> None:
    schema = store[schema_id]
    with pytest.raises(jsonschema.ValidationError):
        Draft202012Validator(schema, registry=registry).validate(frame)


@pytest.fixture(scope="module")
def store_and_registry():
    store = _load_all()
    return store, _registry(store)


def test_inbound_hello_ok(store_and_registry):
    store, reg = store_and_registry
    _validate(
        store,
        reg,
        "https://blemees/schemas/inbound/agent.hello.json",
        {"type": "agent.hello", "protocol": "blemees-agent/1", "client": "test/0"},
    )


def test_inbound_hello_rejects_wrong_protocol(store_and_registry):
    store, reg = store_and_registry
    _assert_invalid(
        store,
        reg,
        "https://blemees/schemas/inbound/agent.hello.json",
        {"type": "agent.hello", "protocol": "blemees/2"},
    )


def test_inbound_open_minimal_claude(store_and_registry):
    store, reg = store_and_registry
    _validate(
        store,
        reg,
        "https://blemees/schemas/inbound/agent.open.json",
        {
            "type": "agent.open",
            "session_id": "s1",
            "backend": "claude",
            "options": {"claude": {}},
        },
    )


def test_inbound_open_minimal_codex(store_and_registry):
    store, reg = store_and_registry
    _validate(
        store,
        reg,
        "https://blemees/schemas/inbound/agent.open.json",
        {
            "type": "agent.open",
            "session_id": "s1",
            "backend": "codex",
            "options": {"codex": {}},
        },
    )


def test_inbound_open_rejects_unknown_backend(store_and_registry):
    store, reg = store_and_registry
    _assert_invalid(
        store,
        reg,
        "https://blemees/schemas/inbound/agent.open.json",
        {
            "type": "agent.open",
            "session_id": "s1",
            "backend": "anthropic",
            "options": {},
        },
    )


def test_inbound_open_rejects_top_level_legacy_field(store_and_registry):
    """`model` and friends used to live at the top of agent.open in
    legacy protocols; in blemees-agent/1 they belong under options.claude.*."""
    store, reg = store_and_registry
    _assert_invalid(
        store,
        reg,
        "https://blemees/schemas/inbound/agent.open.json",
        {
            "type": "agent.open",
            "session_id": "s1",
            "backend": "claude",
            "options": {"claude": {}},
            "model": "sonnet",
        },
    )


def test_options_claude_rejects_unsafe_flag(store_and_registry):
    store, reg = store_and_registry
    _assert_invalid(
        store,
        reg,
        "https://blemees/schemas/inbound/options.claude.json",
        {"dangerously_skip_permissions": True},
    )


def test_options_claude_rejects_input_format(store_and_registry):
    store, reg = store_and_registry
    _assert_invalid(
        store,
        reg,
        "https://blemees/schemas/inbound/options.claude.json",
        {"input_format": "text"},
    )


def test_options_codex_rejects_unknown_key(store_and_registry):
    store, reg = store_and_registry
    _assert_invalid(
        store,
        reg,
        "https://blemees/schemas/inbound/options.codex.json",
        {"approval_policy": "never"},  # underscore form — codex uses approval-policy
    )


def test_options_codex_accepts_full_set(store_and_registry):
    store, reg = store_and_registry
    _validate(
        store,
        reg,
        "https://blemees/schemas/inbound/options.codex.json",
        {
            "model": "gpt-5.2-codex",
            "profile": "default",
            "cwd": "/home/u/proj",
            "sandbox": "read-only",
            "approval-policy": "never",
            "developer-instructions": "be terse",
            "config": {"model_reasoning_effort": "medium"},
            "include_raw_events": False,
        },
    )


def test_inbound_agent_user_string_content(store_and_registry):
    store, reg = store_and_registry
    _validate(
        store,
        reg,
        "https://blemees/schemas/inbound/agent.user.json",
        {
            "type": "agent.user",
            "session_id": "s1",
            "message": {"role": "user", "content": "hi"},
        },
    )


def test_inbound_agent_user_multimodal_content(store_and_registry):
    store, reg = store_and_registry
    _validate(
        store,
        reg,
        "https://blemees/schemas/inbound/agent.user.json",
        {
            "type": "agent.user",
            "session_id": "s1",
            "message": {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is this?"},
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": "..."},
                    },
                ],
            },
        },
    )


def test_inbound_agent_user_rejects_wrong_role(store_and_registry):
    store, reg = store_and_registry
    _assert_invalid(
        store,
        reg,
        "https://blemees/schemas/inbound/agent.user.json",
        {
            "type": "agent.user",
            "session_id": "s1",
            "message": {"role": "assistant", "content": "x"},
        },
    )


def test_inbound_agent_user_rejects_legacy_text_shorthand(store_and_registry):
    store, reg = store_and_registry
    _assert_invalid(
        store,
        reg,
        "https://blemees/schemas/inbound/agent.user.json",
        {"type": "agent.user", "session_id": "s1", "text": "hi"},
    )


def test_inbound_list_sessions_empty_body_ok(store_and_registry):
    """Empty body is valid at the schema level — parser-side defaulting
    fills in `live: true`. The parser is the one that rejects
    `{"live": false}` without `cwd`; we don't try to encode that
    cross-field constraint in JSON Schema."""
    store, reg = store_and_registry
    _validate(
        store,
        reg,
        "https://blemees/schemas/inbound/agent.list_sessions.json",
        {"type": "agent.list_sessions"},
    )


def test_inbound_list_sessions_with_cwd_and_live(store_and_registry):
    store, reg = store_and_registry
    _validate(
        store,
        reg,
        "https://blemees/schemas/inbound/agent.list_sessions.json",
        {"type": "agent.list_sessions", "cwd": "/proj", "live": True},
    )


def test_inbound_list_sessions_rejects_non_bool_live(store_and_registry):
    store, reg = store_and_registry
    _assert_invalid(
        store,
        reg,
        "https://blemees/schemas/inbound/agent.list_sessions.json",
        {"type": "agent.list_sessions", "live": "yes"},
    )


def test_inbound_list_sessions_rejects_empty_cwd(store_and_registry):
    store, reg = store_and_registry
    _assert_invalid(
        store,
        reg,
        "https://blemees/schemas/inbound/agent.list_sessions.json",
        {"type": "agent.list_sessions", "cwd": ""},
    )


def test_outbound_hello_ack_ok(store_and_registry):
    store, reg = store_and_registry
    _validate(
        store,
        reg,
        "https://blemees/schemas/outbound/agent.hello_ack.json",
        {
            "type": "agent.hello_ack",
            "daemon": "blemeesd/0.1.0",
            "protocol": "blemees-agent/1",
            "pid": 12345,
            "backends": {"claude": "2.1.118", "codex": "0.125.0"},
        },
    )


def test_outbound_hello_ack_allows_partial_backends(store_and_registry):
    """The daemon may detect only some backends at startup; missing ones drop out."""
    store, reg = store_and_registry
    _validate(
        store,
        reg,
        "https://blemees/schemas/outbound/agent.hello_ack.json",
        {
            "type": "agent.hello_ack",
            "daemon": "blemeesd/0.1.0",
            "protocol": "blemees-agent/1",
            "pid": 12345,
            "backends": {"claude": "2.1.118"},
        },
    )


def test_outbound_opened_carries_backend_and_last_seq(store_and_registry):
    store, reg = store_and_registry
    _validate(
        store,
        reg,
        "https://blemees/schemas/outbound/agent.opened.json",
        {
            "type": "agent.opened",
            "id": "r1",
            "session_id": "s1",
            "backend": "claude",
            "native_session_id": "s1",
            "subprocess_pid": 9999,
            "last_seq": 0,
        },
    )


def test_outbound_error_enum(store_and_registry):
    store, reg = store_and_registry
    _validate(
        store,
        reg,
        "https://blemees/schemas/outbound/agent.error.json",
        {"type": "agent.error", "code": "invalid_message", "message": "bad field"},
    )
    _validate(
        store,
        reg,
        "https://blemees/schemas/outbound/agent.error.json",
        {
            "type": "agent.error",
            "code": "auth_failed",
            "backend": "codex",
            "message": "run `codex login`",
        },
    )
    # Old codes are gone in blemees-agent/1.
    _assert_invalid(
        store,
        reg,
        "https://blemees/schemas/outbound/agent.error.json",
        {"type": "agent.error", "code": "claude_crashed", "message": "x"},
    )
    _assert_invalid(
        store,
        reg,
        "https://blemees/schemas/outbound/agent.error.json",
        {"type": "agent.error", "code": "oauth_expired", "message": "x"},
    )
    _assert_invalid(
        store,
        reg,
        "https://blemees/schemas/outbound/agent.error.json",
        {"type": "agent.error", "code": "not_a_known_code", "message": "x"},
    )


def test_outbound_replay_gap_shape(store_and_registry):
    store, reg = store_and_registry
    _validate(
        store,
        reg,
        "https://blemees/schemas/outbound/agent.replay_gap.json",
        {
            "type": "agent.replay_gap",
            "session_id": "s1",
            "since_seq": 42,
            "first_available_seq": 71,
        },
    )


def test_outbound_session_taken_ok(store_and_registry):
    store, reg = store_and_registry
    _validate(
        store,
        reg,
        "https://blemees/schemas/outbound/agent.session_taken.json",
        {"type": "agent.session_taken", "session_id": "s1", "by_peer_pid": 12345},
    )


def test_outbound_agent_event_system_init(store_and_registry):
    store, reg = store_and_registry
    schema_id = "https://blemees/schemas/outbound/agent.event.json"
    _validate(
        store,
        reg,
        schema_id,
        {
            "type": "agent.system_init",
            "session_id": "s1",
            "seq": 1,
            "backend": "claude",
            "model": "claude-sonnet-4-6",
            "tools": ["Bash", "Read", "Edit"],
            "native_session_id": "s1",
        },
    )


def test_outbound_agent_event_delta(store_and_registry):
    store, reg = store_and_registry
    schema_id = "https://blemees/schemas/outbound/agent.event.json"
    _validate(
        store,
        reg,
        schema_id,
        {
            "type": "agent.delta",
            "session_id": "s1",
            "seq": 2,
            "backend": "codex",
            "kind": "text",
            "text": "Hello",
            "item_id": "msg_xyz",
        },
    )


def test_outbound_agent_event_result_with_usage(store_and_registry):
    store, reg = store_and_registry
    schema_id = "https://blemees/schemas/outbound/agent.event.json"
    _validate(
        store,
        reg,
        schema_id,
        {
            "type": "agent.result",
            "session_id": "s1",
            "seq": 5,
            "backend": "codex",
            "subtype": "success",
            "duration_ms": 4371,
            "num_turns": 1,
            "turn_id": "3",
            "usage": {
                "input_tokens": 11761,
                "output_tokens": 28,
                "cache_read_input_tokens": 4480,
                "reasoning_output_tokens": 20,
            },
        },
    )


def test_outbound_agent_event_rejects_non_agent_prefix(store_and_registry):
    store, reg = store_and_registry
    schema_id = "https://blemees/schemas/outbound/agent.event.json"
    _assert_invalid(
        store,
        reg,
        schema_id,
        {"type": "claude.stream_event", "session_id": "s1", "seq": 1, "backend": "claude"},
    )


def test_outbound_agent_event_requires_seq_and_backend(store_and_registry):
    store, reg = store_and_registry
    schema_id = "https://blemees/schemas/outbound/agent.event.json"
    _assert_invalid(
        store,
        reg,
        schema_id,
        {"type": "agent.system_init", "session_id": "s1", "backend": "claude"},
    )
    _assert_invalid(
        store,
        reg,
        schema_id,
        {"type": "agent.system_init", "session_id": "s1", "seq": 1},
    )


def test_outbound_agent_event_delta_requires_kind(store_and_registry):
    store, reg = store_and_registry
    schema_id = "https://blemees/schemas/outbound/agent.event.json"
    _assert_invalid(
        store,
        reg,
        schema_id,
        {
            "type": "agent.delta",
            "session_id": "s1",
            "seq": 2,
            "backend": "claude",
            "text": "hi",
        },
    )


def test_outbound_sessions_listing_with_backend(store_and_registry):
    store, reg = store_and_registry
    _validate(
        store,
        reg,
        "https://blemees/schemas/outbound/agent.sessions.json",
        {
            "type": "agent.sessions",
            "id": "r1",
            "cwd": "/home/u/proj",
            "sessions": [
                {
                    "session_id": "abc",
                    "backend": "claude",
                    "attached": False,
                    "mtime_ms": 1745000000000,
                    "size": 4321,
                    "preview": "fix the bug",
                },
                {"session_id": "live", "backend": "codex", "attached": True},
            ],
        },
    )


def test_outbound_sessions_live_only_omits_cwd(store_and_registry):
    """Whole-daemon (live-only) replies omit `cwd` and carry the rich
    live fields on each row."""
    store, reg = store_and_registry
    _validate(
        store,
        reg,
        "https://blemees/schemas/outbound/agent.sessions.json",
        {
            "type": "agent.sessions",
            "id": "r1",
            "sessions": [
                {
                    "session_id": "abc",
                    "backend": "claude",
                    "attached": True,
                    "cwd": "/home/u/proj",
                    "model": "claude-sonnet-4-6",
                    "title": "refactor utils.py",
                    "started_at_ms": 1745000000000,
                    "last_active_at_ms": 1745000123000,
                    "owner_pid": 12345,
                    "last_seq": 47,
                    "turn_active": False,
                },
            ],
        },
    )


def test_outbound_session_closed_owner_closed(store_and_registry):
    store, reg = store_and_registry
    _validate(
        store,
        reg,
        "https://blemees/schemas/outbound/agent.session_closed.json",
        {"type": "agent.session_closed", "session_id": "s1", "reason": "owner_closed"},
    )


def test_outbound_session_closed_rejects_unknown_reason(store_and_registry):
    """Reserved future reasons are added by extending the enum, not by
    accepting whatever string a misbehaving daemon sends."""
    store, reg = store_and_registry
    _assert_invalid(
        store,
        reg,
        "https://blemees/schemas/outbound/agent.session_closed.json",
        {"type": "agent.session_closed", "session_id": "s1", "reason": "made_up"},
    )


def test_outbound_session_closed_requires_reason(store_and_registry):
    store, reg = store_and_registry
    _assert_invalid(
        store,
        reg,
        "https://blemees/schemas/outbound/agent.session_closed.json",
        {"type": "agent.session_closed", "session_id": "s1"},
    )
