"""Wire protocol codec for blemees-agentd (spec §5).

Responsibilities:
    * Encode/decode newline-delimited JSON frames.
    * Validate ``blemeesd.*`` and ``agent.user`` control messages into
      typed dataclasses.
    * Validate the per-backend ``options.<backend>`` block — backend
      specific knob handling and argv assembly live in
      ``blemees_agent.backends.<backend>``, not here.

All dataclasses are immutable and carry only the fields required by the
dispatcher. Inbound frames whose schemas set ``additionalProperties: false``
are rejected when unknown keys are present.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

from . import PROTOCOL_VERSION
from .backends import KNOWN_BACKENDS
from .errors import (
    OversizeMessageError,
    ProtocolError,
    UnknownBackendError,
)

DEFAULT_MAX_LINE_BYTES = 16 * 1024 * 1024


# ---------------------------------------------------------------------------
# Outbound helpers (daemon → client).
# ---------------------------------------------------------------------------


def encode(obj: dict[str, Any]) -> bytes:
    """Encode a message as a single UTF-8 JSON line (with trailing ``\\n``)."""
    # ``ensure_ascii=False`` keeps non-ASCII text compact and still valid JSON.
    return (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def hello_ack(daemon_version: str, pid: int, backends: dict[str, str]) -> dict[str, Any]:
    return {
        "type": "blemeesd.hello_ack",
        "daemon": f"blemeesd/{daemon_version}",
        "protocol": PROTOCOL_VERSION,
        "pid": pid,
        "backends": dict(backends),
    }


def error_frame(
    code: str,
    message: str,
    *,
    id: str | None = None,
    session_id: str | None = None,
    backend: str | None = None,
) -> dict[str, Any]:
    frame: dict[str, Any] = {"type": "blemeesd.error", "code": code, "message": message}
    if id is not None:
        frame["id"] = id
    if session_id is not None:
        frame["session_id"] = session_id
    if backend is not None:
        frame["backend"] = backend
    return frame


# ---------------------------------------------------------------------------
# Inbound parsing.
# ---------------------------------------------------------------------------


def parse_line(line: bytes, *, max_bytes: int = DEFAULT_MAX_LINE_BYTES) -> dict[str, Any]:
    """Parse a single wire line; raises :class:`ProtocolError` on bad input."""
    if len(line) > max_bytes:
        raise OversizeMessageError(max_bytes)
    try:
        text = line.decode("utf-8")
    except UnicodeDecodeError as exc:  # pragma: no cover - trivial
        raise ProtocolError(f"invalid utf-8: {exc}") from exc
    text = text.rstrip("\r\n")
    if not text:
        raise ProtocolError("empty frame")
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"invalid json: {exc.msg}") from exc
    if not isinstance(obj, dict):
        raise ProtocolError("frame must be a JSON object")
    if "type" not in obj or not isinstance(obj["type"], str):
        raise ProtocolError("missing string 'type'")
    return obj


# ---------------------------------------------------------------------------
# Strict-key helper (mirrors ``additionalProperties: false`` in schemas).
# ---------------------------------------------------------------------------


def _reject_extra_keys(obj: dict[str, Any], allowed: frozenset[str]) -> None:
    """Raise :class:`ProtocolError` when *obj* contains keys not in *allowed*."""
    extra = obj.keys() - allowed
    if extra:
        field = next(iter(sorted(extra)))
        raise ProtocolError(f"unexpected field: {field!r}")


# ---------------------------------------------------------------------------
# Typed control-message dataclasses.
# ---------------------------------------------------------------------------


@dataclasses.dataclass(slots=True)
class HelloMessage:
    client: str | None
    protocol: str


@dataclasses.dataclass(slots=True)
class OpenMessage:
    id: str | None
    session_id: str
    backend: str
    options: dict[str, Any]
    resume: bool
    last_seen_seq: int | None = None


@dataclasses.dataclass(slots=True)
class UserMessage:
    session_id: str
    message: dict[str, Any]


@dataclasses.dataclass(slots=True)
class InterruptMessage:
    session_id: str


@dataclasses.dataclass(slots=True)
class CloseMessage:
    id: str | None
    session_id: str
    delete: bool


@dataclasses.dataclass(slots=True)
class ListSessionsMessage:
    id: str | None
    cwd: str | None
    # Tri-state: ``True`` includes only live sessions, ``False`` includes
    # only non-live (cold disk) sessions, ``None`` (default) includes both.
    live: bool | None


@dataclasses.dataclass(slots=True)
class WatchMessage:
    id: str | None
    session_id: str
    last_seen_seq: int | None


@dataclasses.dataclass(slots=True)
class UnwatchMessage:
    id: str | None
    session_id: str


@dataclasses.dataclass(slots=True)
class SessionInfoMessage:
    id: str | None
    session_id: str


_MISSING: Any = object()  # sentinel for optional fields not present in the wire frame


@dataclasses.dataclass(slots=True)
class PingMessage:
    id: str | None
    data: Any  # opaque; echoed back on pong; _MISSING means key was absent


@dataclasses.dataclass(slots=True)
class StatusMessage:
    id: str | None


def parse_hello(obj: dict[str, Any]) -> HelloMessage:
    _reject_extra_keys(obj, frozenset({"type", "protocol", "client"}))
    protocol = obj.get("protocol")
    if not isinstance(protocol, str):
        raise ProtocolError("hello missing 'protocol'")
    client = obj.get("client")
    if client is not None and not isinstance(client, str):
        raise ProtocolError("'client' must be a string")
    return HelloMessage(client=client, protocol=protocol)


_OPEN_TOP_LEVEL = frozenset(
    {"type", "id", "session_id", "backend", "resume", "last_seen_seq", "options"}
)


def parse_open(obj: dict[str, Any]) -> OpenMessage:
    _reject_extra_keys(obj, _OPEN_TOP_LEVEL)

    session_id = obj.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ProtocolError("open requires non-empty 'session_id'")

    backend = obj.get("backend")
    if not isinstance(backend, str) or not backend:
        raise ProtocolError("open requires non-empty 'backend'")
    if backend not in KNOWN_BACKENDS:
        raise UnknownBackendError(backend)

    options_field = obj.get("options")
    if options_field is None:
        raise ProtocolError("open requires 'options'")
    if not isinstance(options_field, dict):
        raise ProtocolError("'options' must be an object")
    # The schema rejects sibling backends here too, but the daemon is
    # permissive: we read only the matching backend's block.
    backend_options = options_field.get(backend)
    if backend_options is None:
        backend_options = {}
    if not isinstance(backend_options, dict):
        raise ProtocolError(f"'options.{backend}' must be an object")
    # Reject sibling-backend blocks so malformed clients don't go silent.
    for key in options_field:
        if key not in KNOWN_BACKENDS:
            raise ProtocolError(f"unknown options block: {key!r}")

    resume = bool(obj.get("resume", False))

    req_id = obj.get("id")
    if req_id is not None and not isinstance(req_id, str):
        raise ProtocolError("'id' must be a string")

    last_seen_seq = obj.get("last_seen_seq")
    if last_seen_seq is not None:
        if not isinstance(last_seen_seq, int) or last_seen_seq < 0:
            raise ProtocolError("'last_seen_seq' must be a non-negative integer")

    return OpenMessage(
        id=req_id,
        session_id=session_id,
        backend=backend,
        options=backend_options,
        resume=resume,
        last_seen_seq=last_seen_seq,
    )


def parse_user(obj: dict[str, Any]) -> UserMessage:
    _reject_extra_keys(obj, frozenset({"type", "session_id", "message"}))
    session_id = obj.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ProtocolError("user requires 'session_id'")
    message = obj.get("message")
    if not isinstance(message, dict):
        raise ProtocolError("user requires 'message' object")
    role = message.get("role")
    if role != "user":
        raise ProtocolError("message.role must be 'user'")
    content = message.get("content")
    if not isinstance(content, (str, list)):
        raise ProtocolError("message.content must be a string or array")
    return UserMessage(session_id=session_id, message=message)


def parse_interrupt(obj: dict[str, Any]) -> InterruptMessage:
    _reject_extra_keys(obj, frozenset({"type", "session_id"}))
    session_id = obj.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ProtocolError("interrupt requires 'session_id'")
    return InterruptMessage(session_id=session_id)


def parse_close(obj: dict[str, Any]) -> CloseMessage:
    _reject_extra_keys(obj, frozenset({"type", "id", "session_id", "delete"}))
    session_id = obj.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ProtocolError("close requires 'session_id'")
    delete = bool(obj.get("delete", False))
    req_id = obj.get("id")
    if req_id is not None and not isinstance(req_id, str):
        raise ProtocolError("'id' must be a string")
    return CloseMessage(id=req_id, session_id=session_id, delete=delete)


def parse_list_sessions(obj: dict[str, Any]) -> ListSessionsMessage:
    """Parse ``blemeesd.list_sessions``.

    ``cwd`` and ``live`` are independent, fully-composable filters.
    Omitting a filter means "no filter on that axis":

    * ``cwd`` set — restrict to that working directory; absent — every cwd.
    * ``live: true`` — only sessions currently live in the daemon.
    * ``live: false`` — only sessions that exist on disk but are not
      currently live (cold sessions).
    * ``live`` absent — both live and cold; the on-disk and live-overlay
      passes are merged by ``(backend, session_id)``.

    Empty body therefore means "every session, everywhere" — including
    a full scan of ``~/.claude/projects/`` and ``~/.codex/sessions/``.
    Callers who want the cheap watch-picker query should pass
    ``live:true``.
    """
    _reject_extra_keys(obj, frozenset({"type", "id", "cwd", "live"}))

    cwd_field = obj.get("cwd")
    if cwd_field is not None and (not isinstance(cwd_field, str) or not cwd_field):
        raise ProtocolError("'cwd' must be a non-empty string when set")

    live_field = obj.get("live")
    if live_field is not None and not isinstance(live_field, bool):
        raise ProtocolError("'live' must be a boolean")

    req_id = obj.get("id")
    if req_id is not None and not isinstance(req_id, str):
        raise ProtocolError("'id' must be a string")

    return ListSessionsMessage(id=req_id, cwd=cwd_field, live=live_field)


def parse_ping(obj: dict[str, Any]) -> PingMessage:
    _reject_extra_keys(obj, frozenset({"type", "id", "data"}))
    req_id = obj.get("id")
    if req_id is not None and not isinstance(req_id, str):
        raise ProtocolError("'id' must be a string")
    return PingMessage(id=req_id, data=obj.get("data", _MISSING))


def parse_status(obj: dict[str, Any]) -> StatusMessage:
    _reject_extra_keys(obj, frozenset({"type", "id"}))
    req_id = obj.get("id")
    if req_id is not None and not isinstance(req_id, str):
        raise ProtocolError("'id' must be a string")
    return StatusMessage(id=req_id)


def parse_watch(obj: dict[str, Any]) -> WatchMessage:
    _reject_extra_keys(obj, frozenset({"type", "id", "session_id", "last_seen_seq"}))
    session_id = obj.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ProtocolError("watch requires 'session_id'")
    req_id = obj.get("id")
    if req_id is not None and not isinstance(req_id, str):
        raise ProtocolError("'id' must be a string")
    last_seen_seq = obj.get("last_seen_seq")
    if last_seen_seq is not None and (not isinstance(last_seen_seq, int) or last_seen_seq < 0):
        raise ProtocolError("'last_seen_seq' must be a non-negative integer")
    return WatchMessage(id=req_id, session_id=session_id, last_seen_seq=last_seen_seq)


def parse_unwatch(obj: dict[str, Any]) -> UnwatchMessage:
    _reject_extra_keys(obj, frozenset({"type", "id", "session_id"}))
    session_id = obj.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ProtocolError("unwatch requires 'session_id'")
    req_id = obj.get("id")
    if req_id is not None and not isinstance(req_id, str):
        raise ProtocolError("'id' must be a string")
    return UnwatchMessage(id=req_id, session_id=session_id)


def parse_session_info(obj: dict[str, Any]) -> SessionInfoMessage:
    _reject_extra_keys(obj, frozenset({"type", "id", "session_id"}))
    session_id = obj.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ProtocolError("session_info requires 'session_id'")
    req_id = obj.get("id")
    if req_id is not None and not isinstance(req_id, str):
        raise ProtocolError("'id' must be a string")
    return SessionInfoMessage(id=req_id, session_id=session_id)
