"""Validate the JSON Schemas in ``schemas/`` parse, meta-validate, and
accept canonical example frames. If ``jsonschema`` is not installed the
test is skipped rather than failed — schemas remain a contract even if
the dev dep isn't available.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


jsonschema = pytest.importorskip("jsonschema")
from jsonschema import Draft202012Validator
from referencing import Registry, Resource


SCHEMAS_DIR = Path(__file__).resolve().parents[2] / "schemas"


def _load_all() -> dict[str, dict]:
    return {
        json.loads(p.read_text())["$id"]: json.loads(p.read_text())
        for p in SCHEMAS_DIR.rglob("*.json")
    }


def _registry(store: dict[str, dict]) -> Registry:
    reg = Registry()
    for uri, schema in store.items():
        reg = reg.with_resource(uri, Resource.from_contents(schema))
    return reg


# ---------------------------------------------------------------------------
# Meta-validation
# ---------------------------------------------------------------------------

def test_every_schema_parses_and_declares_id_and_schema():
    for path in SCHEMAS_DIR.rglob("*.json"):
        obj = json.loads(path.read_text())
        assert obj.get("$schema") == "https://json-schema.org/draft/2020-12/schema", path
        assert obj.get("$id", "").startswith("https://blemees/schemas/"), path


def test_every_schema_is_valid_against_draft_2020_12_metaschema():
    for path in SCHEMAS_DIR.rglob("*.json"):
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
        "https://blemees/schemas/inbound/blemeesd.hello.json",
        {"type": "blemeesd.hello", "protocol": "blemees/1", "client": "test/0"},
    )


def test_inbound_hello_rejects_wrong_protocol(store_and_registry):
    store, reg = store_and_registry
    _assert_invalid(
        store,
        reg,
        "https://blemees/schemas/inbound/blemeesd.hello.json",
        {"type": "blemeesd.hello", "protocol": "blemees/99"},
    )


def test_inbound_open_minimal(store_and_registry):
    store, reg = store_and_registry
    _validate(
        store,
        reg,
        "https://blemees/schemas/inbound/blemeesd.open.json",
        {"type": "blemeesd.open", "session": "s1"},
    )


def test_inbound_open_rejects_unsafe_flag(store_and_registry):
    store, reg = store_and_registry
    _assert_invalid(
        store,
        reg,
        "https://blemees/schemas/inbound/blemeesd.open.json",
        {
            "type": "blemeesd.open",
            "session": "s1",
            "dangerously_skip_permissions": True,
        },
    )


def test_inbound_open_rejects_input_format(store_and_registry):
    store, reg = store_and_registry
    _assert_invalid(
        store,
        reg,
        "https://blemees/schemas/inbound/blemeesd.open.json",
        {"type": "blemeesd.open", "session": "s1", "input_format": "text"},
    )


def test_inbound_claude_user_string_content(store_and_registry):
    store, reg = store_and_registry
    _validate(
        store,
        reg,
        "https://blemees/schemas/inbound/claude.user.json",
        {
            "type": "claude.user",
            "session": "s1",
            "message": {"role": "user", "content": "hi"},
        },
    )


def test_inbound_claude_user_multimodal_content(store_and_registry):
    store, reg = store_and_registry
    _validate(
        store,
        reg,
        "https://blemees/schemas/inbound/claude.user.json",
        {
            "type": "claude.user",
            "session": "s1",
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


def test_inbound_claude_user_rejects_wrong_role(store_and_registry):
    store, reg = store_and_registry
    _assert_invalid(
        store,
        reg,
        "https://blemees/schemas/inbound/claude.user.json",
        {
            "type": "claude.user",
            "session": "s1",
            "message": {"role": "assistant", "content": "x"},
        },
    )


def test_inbound_claude_user_rejects_legacy_text_shorthand(store_and_registry):
    store, reg = store_and_registry
    _assert_invalid(
        store,
        reg,
        "https://blemees/schemas/inbound/claude.user.json",
        {"type": "claude.user", "session": "s1", "text": "hi"},
    )


def test_inbound_list_sessions_requires_cwd(store_and_registry):
    store, reg = store_and_registry
    _assert_invalid(
        store,
        reg,
        "https://blemees/schemas/inbound/blemeesd.list_sessions.json",
        {"type": "blemeesd.list_sessions"},
    )


def test_outbound_hello_ack_ok(store_and_registry):
    store, reg = store_and_registry
    _validate(
        store,
        reg,
        "https://blemees/schemas/outbound/blemeesd.hello_ack.json",
        {
            "type": "blemeesd.hello_ack",
            "daemon": "blemeesd/0.1.0",
            "protocol": "blemees/1",
            "pid": 12345,
            "claude_version": "2.1.118",
        },
    )


def test_outbound_opened_carries_last_seq(store_and_registry):
    store, reg = store_and_registry
    _validate(
        store,
        reg,
        "https://blemees/schemas/outbound/blemeesd.opened.json",
        {
            "type": "blemeesd.opened",
            "id": "r1",
            "session": "s1",
            "subprocess_pid": 9999,
            "last_seq": 0,
        },
    )


def test_outbound_error_enum(store_and_registry):
    store, reg = store_and_registry
    _validate(
        store,
        reg,
        "https://blemees/schemas/outbound/blemeesd.error.json",
        {"type": "blemeesd.error", "code": "invalid_message", "message": "bad field"},
    )
    _assert_invalid(
        store,
        reg,
        "https://blemees/schemas/outbound/blemeesd.error.json",
        {"type": "blemeesd.error", "code": "not_a_known_code", "message": "x"},
    )


def test_outbound_replay_gap_shape(store_and_registry):
    store, reg = store_and_registry
    _validate(
        store,
        reg,
        "https://blemees/schemas/outbound/blemeesd.replay_gap.json",
        {
            "type": "blemeesd.replay_gap",
            "session": "s1",
            "since_seq": 42,
            "first_available_seq": 71,
        },
    )


def test_outbound_session_taken_ok(store_and_registry):
    store, reg = store_and_registry
    _validate(
        store,
        reg,
        "https://blemees/schemas/outbound/blemeesd.session_taken.json",
        {"type": "blemeesd.session_taken", "session": "s1", "by_peer_pid": 12345},
    )


def test_outbound_claude_event_envelope(store_and_registry):
    store, reg = store_and_registry
    schema_id = "https://blemees/schemas/outbound/claude.event.json"
    _validate(
        store,
        reg,
        schema_id,
        {
            "type": "claude.stream_event",
            "session": "s1",
            "seq": 3,
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "hi"},
            },
        },
    )
    # Rejects non-claude. prefix.
    _assert_invalid(
        store,
        reg,
        schema_id,
        {"type": "system", "session": "s1", "seq": 1},
    )
    # Requires seq for session-stream frames.
    _assert_invalid(
        store,
        reg,
        schema_id,
        {"type": "claude.system", "session": "s1"},
    )


def test_outbound_sessions_listing(store_and_registry):
    store, reg = store_and_registry
    _validate(
        store,
        reg,
        "https://blemees/schemas/outbound/blemeesd.sessions.json",
        {
            "type": "blemeesd.sessions",
            "id": "r1",
            "cwd": "/home/u/proj",
            "sessions": [
                {
                    "session": "abc",
                    "attached": False,
                    "mtime_ms": 1745000000000,
                    "size": 4321,
                    "preview": "fix the bug",
                },
                {"session": "live", "attached": True},
            ],
        },
    )
