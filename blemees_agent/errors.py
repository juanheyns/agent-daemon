"""Typed exceptions and error codes for blemeesd.

Each error-code string appears in a `blemeesd.error` frame's `code` field.
See spec §5.10 for the complete table.
"""

from __future__ import annotations

# Error codes (see spec §5.10).
PROTOCOL_MISMATCH = "protocol_mismatch"
INVALID_MESSAGE = "invalid_message"
UNKNOWN_MESSAGE = "unknown_message"
UNKNOWN_BACKEND = "unknown_backend"
UNSAFE_FLAG = "unsafe_flag"
SESSION_UNKNOWN = "session_unknown"
SESSION_EXISTS = "session_exists"
SESSION_BUSY = "session_busy"
SPAWN_FAILED = "spawn_failed"
BACKEND_CRASHED = "backend_crashed"
AUTH_FAILED = "auth_failed"
OVERSIZE_MESSAGE = "oversize_message"
SLOW_CONSUMER = "slow_consumer"
DAEMON_SHUTDOWN = "daemon_shutdown"
INTERNAL = "internal"


# Codes that require tearing down the client connection once emitted.
FATAL_CODES = frozenset(
    {
        PROTOCOL_MISMATCH,
        OVERSIZE_MESSAGE,
        SLOW_CONSUMER,
        DAEMON_SHUTDOWN,
    }
)


class BlemeesError(Exception):
    """Base exception carrying a machine-readable error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    @property
    def fatal(self) -> bool:
        return self.code in FATAL_CODES


class ProtocolError(BlemeesError):
    def __init__(self, message: str, code: str = INVALID_MESSAGE) -> None:
        super().__init__(code, message)


class UnsafeFlagError(BlemeesError):
    def __init__(self, flag: str) -> None:
        super().__init__(UNSAFE_FLAG, f"refused flag: {flag}")
        self.flag = flag


class UnknownBackendError(BlemeesError):
    def __init__(self, backend: str) -> None:
        super().__init__(UNKNOWN_BACKEND, f"unknown backend: {backend!r}")
        self.backend = backend


class SessionUnknownError(BlemeesError):
    def __init__(self, session: str) -> None:
        super().__init__(SESSION_UNKNOWN, f"no such session: {session}")
        self.session = session


class SessionExistsError(BlemeesError):
    def __init__(self, session: str) -> None:
        super().__init__(SESSION_EXISTS, f"session already open: {session}")
        self.session = session


class SessionBusyError(BlemeesError):
    def __init__(self, session: str) -> None:
        super().__init__(SESSION_BUSY, f"session has a turn in flight: {session}")
        self.session = session


class SpawnFailedError(BlemeesError):
    def __init__(self, message: str) -> None:
        super().__init__(SPAWN_FAILED, message)


class OversizeMessageError(BlemeesError):
    def __init__(self, limit: int) -> None:
        super().__init__(OVERSIZE_MESSAGE, f"frame exceeds {limit} bytes")
        self.limit = limit
