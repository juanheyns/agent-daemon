"""Top-level pytest configuration for the blemees tests.

Auto-skip ``requires_claude``-marked tests unless the caller opts in with
``pytest -m requires_claude``. Matches spec §11.3: these run only against
a real, authenticated ``claude`` CLI.
"""

from __future__ import annotations

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    markexpr = config.getoption("-m") or ""
    if "requires_claude" in markexpr:
        return  # explicit opt-in; let them run.
    skip_marker = pytest.mark.skip(
        reason="requires real authenticated `claude` CLI; run with `-m requires_claude`"
    )
    for item in items:
        if "requires_claude" in item.keywords:
            item.add_marker(skip_marker)
