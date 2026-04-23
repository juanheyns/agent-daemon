"""Structured JSON logging for ccsockd (spec §10).

The daemon emits one JSON object per log line to stderr (or to a file via
``--log-file``). Secrets are never logged at INFO+; at DEBUG they are
redacted to a length summary.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any, IO


LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "warn": logging.WARNING,
    "error": logging.ERROR,
}


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": _format_ts(record.created),
            "level": record.levelname.lower(),
            "event": record.getMessage(),
        }
        extra = getattr(record, "_ccsock_extra", None)
        if isinstance(extra, dict):
            for key, value in extra.items():
                if key not in payload:
                    payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def _format_ts(ts: float) -> str:
    # ISO-8601 with milliseconds; Z suffix for UTC.
    secs = int(ts)
    millis = int((ts - secs) * 1000)
    lt = time.gmtime(secs)
    return f"{time.strftime('%Y-%m-%dT%H:%M:%S', lt)}.{millis:03d}Z"


class StructuredLogger:
    """Thin wrapper around ``logging.Logger`` that attaches structured fields."""

    def __init__(self, logger: logging.Logger, **base: Any) -> None:
        self._logger = logger
        self._base = base

    def bind(self, **fields: Any) -> "StructuredLogger":
        merged = dict(self._base)
        merged.update(fields)
        return StructuredLogger(self._logger, **merged)

    def _log(self, level: int, event: str, **fields: Any) -> None:
        if not self._logger.isEnabledFor(level):
            return
        payload = dict(self._base)
        payload.update(fields)
        self._logger.log(level, event, extra={"_ccsock_extra": payload})

    def debug(self, event: str, **fields: Any) -> None:
        self._log(logging.DEBUG, event, **fields)

    def info(self, event: str, **fields: Any) -> None:
        self._log(logging.INFO, event, **fields)

    def warning(self, event: str, **fields: Any) -> None:
        self._log(logging.WARNING, event, **fields)

    def error(self, event: str, **fields: Any) -> None:
        self._log(logging.ERROR, event, **fields)

    def exception(self, event: str, **fields: Any) -> None:
        payload = dict(self._base)
        payload.update(fields)
        self._logger.exception(event, extra={"_ccsock_extra": payload})


def configure(level: str = "info", log_file: str | None = None) -> StructuredLogger:
    """Configure root-level structured logging; returns a bound logger."""

    root = logging.getLogger("ccsock")
    root.setLevel(LEVELS.get(level.lower(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)
    stream: IO[str]
    if log_file:
        stream = open(log_file, "a", buffering=1, encoding="utf-8")  # line-buffered
    else:
        stream = sys.stderr
    handler = logging.StreamHandler(stream)
    handler.setFormatter(_JsonFormatter())
    root.addHandler(handler)
    root.propagate = False
    return StructuredLogger(root)


def redact(data: str | bytes | None) -> str:
    """Return a fixed-length redaction marker for DEBUG logging of secrets."""
    if data is None:
        return "<none>"
    if isinstance(data, bytes):
        n = len(data)
    else:
        n = len(data)
    return f"<redacted {n} chars>"
