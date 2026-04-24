"""Wire protocol codec for ccsockd (spec §5).

Responsibilities:
    * Encode/decode newline-delimited JSON frames.
    * Validate ``ccsockd.*`` control messages into typed dataclasses.
    * Map `ccsockd.open` fields onto ``claude -p`` CLI flags.
    * Reject unsafe flags.

All dataclasses are immutable and carry only the fields required by the
dispatcher. Unknown fields in inbound messages are tolerated (forward
compatibility), but unrecognised ``ccsockd.*`` types raise.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any, Iterable

from . import PROTOCOL_VERSION
from .errors import (
    INVALID_MESSAGE,
    OversizeMessageError,
    ProtocolError,
    UnsafeFlagError,
)


DEFAULT_MAX_LINE_BYTES = 16 * 1024 * 1024


# ---------------------------------------------------------------------------
# Outbound helpers (daemon → client).
# ---------------------------------------------------------------------------

def encode(obj: dict[str, Any]) -> bytes:
    """Encode a message as a single UTF-8 JSON line (with trailing ``\\n``)."""
    # ``ensure_ascii=False`` keeps non-ASCII text compact and still valid JSON.
    return (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def hello_ack(daemon_version: str, pid: int, claude_version: str | None) -> dict[str, Any]:
    return {
        "type": "ccsockd.hello_ack",
        "daemon": f"ccsockd/{daemon_version}",
        "protocol": PROTOCOL_VERSION,
        "pid": pid,
        "claude_version": claude_version,
    }


def error_frame(
    code: str,
    message: str,
    *,
    id: str | None = None,
    session: str | None = None,
) -> dict[str, Any]:
    frame: dict[str, Any] = {"type": "ccsockd.error", "code": code, "message": message}
    if id is not None:
        frame["id"] = id
    if session is not None:
        frame["session"] = session
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
# Typed control-message dataclasses.
# ---------------------------------------------------------------------------

@dataclasses.dataclass(slots=True)
class HelloMessage:
    client: str | None
    protocol: str


@dataclasses.dataclass(slots=True)
class OpenMessage:
    id: str | None
    session: str
    resume: bool
    fields: dict[str, Any]  # raw validated fields for flag mapping


@dataclasses.dataclass(slots=True)
class UserMessage:
    session: str
    text: str | None
    content: list[Any] | None


@dataclasses.dataclass(slots=True)
class InterruptMessage:
    session: str


@dataclasses.dataclass(slots=True)
class CloseMessage:
    id: str | None
    session: str
    delete: bool


@dataclasses.dataclass(slots=True)
class ListSessionsMessage:
    id: str | None
    cwd: str


def parse_hello(obj: dict[str, Any]) -> HelloMessage:
    protocol = obj.get("protocol")
    if not isinstance(protocol, str):
        raise ProtocolError("hello missing 'protocol'")
    client = obj.get("client")
    if client is not None and not isinstance(client, str):
        raise ProtocolError("'client' must be a string")
    return HelloMessage(client=client, protocol=protocol)


# Fields the daemon refuses outright (spec §5.4).
UNSAFE_FLAG_FIELDS: frozenset[str] = frozenset(
    {
        "dangerously_skip_permissions",
        "allow_dangerously_skip_permissions",
        "bare",
        "continue",
        "continue_",  # python reserved-word-friendly alias
        "from_pr",
    }
)

# Fields also refused if passed literally under their CLI form.
UNSAFE_LITERAL_FLAGS: frozenset[str] = frozenset(
    {
        "--dangerously-skip-permissions",
        "--allow-dangerously-skip-permissions",
        "--bare",
        "--continue",
        "--from-pr",
    }
)


_OPEN_VALID_FIELDS = {
    "id",
    "session",
    "resume",
    "model",
    "system_prompt",
    "append_system_prompt",
    "tools",
    "disallowed_tools",
    "permission_mode",
    "cwd",
    "add_dir",
    "effort",
    "agent",
    "agents",
    "mcp_config",
    "strict_mcp_config",
    "settings",
    "setting_sources",
    "plugin_dir",
    "betas",
    "exclude_dynamic_system_prompt_sections",
    "max_budget_usd",
    "json_schema",
    "fallback_model",
    "session_name",
    "session_persistence",
    "input_format",
    "output_format",
    "include_partial_messages",
    "replay_user_messages",
    "type",
}


def parse_open(obj: dict[str, Any]) -> OpenMessage:
    session = obj.get("session")
    if not isinstance(session, str) or not session:
        raise ProtocolError("open requires non-empty 'session'")
    resume = bool(obj.get("resume", False))

    # Refuse explicit unsafe flag keys.
    for field in obj.keys():
        if field in UNSAFE_FLAG_FIELDS:
            raise UnsafeFlagError(field)

    # Refuse unsafe CLI literals accidentally smuggled through free-form fields.
    for literal in UNSAFE_LITERAL_FLAGS:
        for value in obj.values():
            if isinstance(value, str) and value == literal:
                raise UnsafeFlagError(literal)
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, str) and item == literal:
                        raise UnsafeFlagError(literal)

    fields: dict[str, Any] = {}
    for key, value in obj.items():
        if key in _OPEN_VALID_FIELDS:
            fields[key] = value

    req_id = obj.get("id")
    if req_id is not None and not isinstance(req_id, str):
        raise ProtocolError("'id' must be a string")

    return OpenMessage(id=req_id, session=session, resume=resume, fields=fields)


def parse_user(obj: dict[str, Any]) -> UserMessage:
    session = obj.get("session")
    if not isinstance(session, str) or not session:
        raise ProtocolError("user requires 'session'")
    text = obj.get("text")
    content = obj.get("content")
    if text is not None and not isinstance(text, str):
        raise ProtocolError("'text' must be a string")
    if content is not None and not isinstance(content, list):
        raise ProtocolError("'content' must be an array")
    if text is None and content is None:
        raise ProtocolError("user requires 'text' or 'content'")
    return UserMessage(session=session, text=text, content=content)


def parse_interrupt(obj: dict[str, Any]) -> InterruptMessage:
    session = obj.get("session")
    if not isinstance(session, str) or not session:
        raise ProtocolError("interrupt requires 'session'")
    return InterruptMessage(session=session)


def parse_close(obj: dict[str, Any]) -> CloseMessage:
    session = obj.get("session")
    if not isinstance(session, str) or not session:
        raise ProtocolError("close requires 'session'")
    delete = bool(obj.get("delete", False))
    req_id = obj.get("id")
    if req_id is not None and not isinstance(req_id, str):
        raise ProtocolError("'id' must be a string")
    return CloseMessage(id=req_id, session=session, delete=delete)


def parse_list_sessions(obj: dict[str, Any]) -> ListSessionsMessage:
    cwd = obj.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        raise ProtocolError("list_sessions requires non-empty 'cwd'")
    req_id = obj.get("id")
    if req_id is not None and not isinstance(req_id, str):
        raise ProtocolError("'id' must be a string")
    return ListSessionsMessage(id=req_id, cwd=cwd)


# ---------------------------------------------------------------------------
# Open → argv mapping (spec §5.4 / §6.1).
# ---------------------------------------------------------------------------

def build_claude_argv(
    claude_bin: str,
    open_msg: OpenMessage,
    *,
    for_resume: bool = False,
) -> list[str]:
    """Translate an :class:`OpenMessage` into a ``claude`` argv list.

    Only fields explicitly set on ``open_msg`` produce CLI flags. The daemon
    always forces ``--verbose`` (required by CC when ``--output-format
    stream-json`` is used with ``-p``).

    When ``for_resume`` is true, emits ``--resume <session>``; otherwise
    ``--session-id <session>`` or ``--resume`` per the original open.
    """
    f = open_msg.fields
    argv: list[str] = [claude_bin, "-p", "--verbose"]

    use_resume = for_resume or open_msg.resume
    if use_resume:
        argv += ["--resume", open_msg.session]
    else:
        argv += ["--session-id", open_msg.session]

    # Format flags default to stream-json both ways unless the client overrode.
    input_format = f.get("input_format", "stream-json")
    output_format = f.get("output_format", "stream-json")
    argv += ["--input-format", str(input_format)]
    argv += ["--output-format", str(output_format)]

    def add(flag: str, value: Any) -> None:
        argv.append(flag)
        argv.append(str(value))

    def add_list(flag: str, values: Iterable[Any]) -> None:
        argv.append(flag)
        argv.extend(str(v) for v in values)

    if "model" in f and f["model"] is not None:
        add("--model", f["model"])
    if "system_prompt" in f and f["system_prompt"] is not None:
        add("--system-prompt", f["system_prompt"])
    if "append_system_prompt" in f and f["append_system_prompt"] is not None:
        add("--append-system-prompt", f["append_system_prompt"])
    if "tools" in f and f["tools"] is not None:
        # Empty string is a legitimate value (disable all tools).
        add("--tools", f["tools"])
    if "disallowed_tools" in f and f["disallowed_tools"]:
        add_list("--disallowedTools", f["disallowed_tools"])
    if "permission_mode" in f and f["permission_mode"] is not None:
        add("--permission-mode", f["permission_mode"])
    if "add_dir" in f and f["add_dir"]:
        add_list("--add-dir", f["add_dir"])
    if "effort" in f and f["effort"] is not None:
        add("--effort", f["effort"])
    if "agent" in f and f["agent"] is not None:
        add("--agent", f["agent"])
    if "agents" in f and f["agents"] is not None:
        # Client passes a dict; serialise to JSON.
        if isinstance(f["agents"], str):
            add("--agents", f["agents"])
        else:
            add("--agents", json.dumps(f["agents"], separators=(",", ":")))
    if "mcp_config" in f and f["mcp_config"]:
        add_list("--mcp-config", f["mcp_config"])
    if f.get("strict_mcp_config"):
        argv.append("--strict-mcp-config")
    if "settings" in f and f["settings"] is not None:
        add("--settings", f["settings"])
    if "setting_sources" in f and f["setting_sources"] is not None:
        add("--setting-sources", f["setting_sources"])
    if "plugin_dir" in f and f["plugin_dir"]:
        for item in f["plugin_dir"]:
            add("--plugin-dir", item)
    if "betas" in f and f["betas"]:
        add_list("--betas", f["betas"])
    if f.get("exclude_dynamic_system_prompt_sections"):
        argv.append("--exclude-dynamic-system-prompt-sections")
    if "max_budget_usd" in f and f["max_budget_usd"] is not None:
        add("--max-budget-usd", f["max_budget_usd"])
    if "json_schema" in f and f["json_schema"] is not None:
        if isinstance(f["json_schema"], str):
            add("--json-schema", f["json_schema"])
        else:
            add("--json-schema", json.dumps(f["json_schema"], separators=(",", ":")))
    if "fallback_model" in f and f["fallback_model"] is not None:
        add("--fallback-model", f["fallback_model"])
    if "session_name" in f and f["session_name"] is not None:
        add("-n", f["session_name"])
    if "session_persistence" in f and f["session_persistence"] is False:
        argv.append("--no-session-persistence")
    if f.get("include_partial_messages"):
        argv.append("--include-partial-messages")
    if f.get("replay_user_messages"):
        argv.append("--replay-user-messages")

    return argv


def build_user_stdin_line(session: str, *, text: str | None, content: list[Any] | None) -> bytes:
    """Canonical ``claude`` stream-json input line for a user turn."""
    if content is not None:
        message_content: Any = content
    else:
        message_content = text if text is not None else ""
    payload = {
        "type": "user",
        "message": {"role": "user", "content": message_content},
        "session_id": session,
    }
    return (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
