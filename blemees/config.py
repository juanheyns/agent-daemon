"""Configuration loading for blemeesd.

Precedence (highest first): CLI flag > env var > config file > default.
Config file format is TOML (stdlib ``tomllib``). All fields optional.
See spec §8.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import sys
import tomllib
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_PATH = Path.home() / ".config" / "blemeesd" / "config.toml"


@dataclasses.dataclass(slots=True)
class Config:
    socket_path: str
    claude_bin: str = "claude"
    log_level: str = "info"
    log_file: str | None = None
    max_line_bytes: int = 16 * 1024 * 1024
    idle_timeout_s: int = 900
    session_retention_days: int = 7
    max_sessions_per_connection: int = 32
    max_concurrent_sessions: int = 64
    stderr_rate_lines: int = 50
    stderr_rate_window_s: int = 10
    # Per-session event-stream buffering.
    ring_buffer_size: int = 1024
    # When set, outbound frames are appended to ``<event_log_dir>/<session>.jsonl``.
    # Survives daemon restarts; seeds the ring buffer on reopen. Opt-in.
    event_log_dir: str | None = None


def default_socket_path() -> str:
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return str(Path(xdg) / "blemeesd.sock")
    if sys.platform == "darwin":
        return f"/tmp/blemeesd-{os.getuid()}.sock"
    # Fallback for Linux without XDG_RUNTIME_DIR set.
    return f"/tmp/blemeesd-{os.getuid()}.sock"


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _env_overrides() -> dict[str, Any]:
    mapping = {
        "BLEMEESD_SOCKET": "socket_path",
        "BLEMEESD_CLAUDE": "claude_bin",
        "BLEMEESD_LOG_LEVEL": "log_level",
        "BLEMEESD_LOG_FILE": "log_file",
        "BLEMEESD_MAX_LINE": "max_line_bytes",
        "BLEMEESD_IDLE_TIMEOUT": "idle_timeout_s",
        "BLEMEESD_RING_BUFFER_SIZE": "ring_buffer_size",
        "BLEMEESD_EVENT_LOG_DIR": "event_log_dir",
    }
    out: dict[str, Any] = {}
    for env_name, field in mapping.items():
        if env_name in os.environ:
            out[field] = os.environ[env_name]
    return out


def _coerce(field: dataclasses.Field, value: Any) -> Any:
    if value is None:
        return None
    if field.type is int or field.type == "int":
        return int(value)
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="blemeesd")
    parser.add_argument("--socket", dest="socket_path", help="Unix socket path")
    parser.add_argument("--claude", dest="claude_bin", help="Path to the claude binary")
    parser.add_argument("--log-level", dest="log_level", help="debug|info|warning|error")
    parser.add_argument("--log-file", dest="log_file", help="Log file path (default stderr)")
    parser.add_argument("--config", dest="config_path", help="TOML config file")
    parser.add_argument("--version", action="store_true", help="Print version and exit")
    return parser


def load(argv: list[str] | None = None) -> tuple[Config, bool]:
    """Resolve effective configuration from file + env + CLI.

    Returns ``(config, print_version_and_exit)``.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    cfg_path = Path(args.config_path) if args.config_path else DEFAULT_CONFIG_PATH
    file_values: dict[str, Any] = _load_toml(cfg_path)
    env_values = _env_overrides()
    cli_values = {
        k: v for k, v in vars(args).items()
        if k not in {"config_path", "version"} and v is not None
    }

    merged: dict[str, Any] = {}
    merged.update(file_values)
    merged.update(env_values)
    merged.update(cli_values)
    merged.setdefault("socket_path", default_socket_path())

    fields = {f.name: f for f in dataclasses.fields(Config)}
    cleaned: dict[str, Any] = {}
    for key, value in merged.items():
        if key in fields:
            cleaned[key] = _coerce(fields[key], value)

    return Config(**cleaned), bool(args.version)
