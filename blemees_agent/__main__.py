"""Console entry point: ``python -m blemees_agent`` / ``blemees-agentd``."""

from __future__ import annotations

import asyncio
import sys

from . import __version__
from .config import load
from .daemon import run_daemon
from .logging import configure


def main() -> int:
    config, want_version = load()
    if want_version:
        print(f"blemees-agentd {__version__}")
        return 0
    logger = configure(config.log_level, config.log_file)
    try:
        return asyncio.run(run_daemon(config, logger))
    except KeyboardInterrupt:  # pragma: no cover - defensive
        return 0


if __name__ == "__main__":
    sys.exit(main())
