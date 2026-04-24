"""Top-level pytest configuration for the blemees tests.

Auto-skip ``requires_claude``-marked tests unless the caller opts in with
``pytest -m requires_claude``. Matches spec §11.3: these run only against
a real, authenticated ``claude`` CLI.
"""

from __future__ import annotations

import pytest

try:
    from _pytest.mark.expression import Expression as _MarkExpression

    def _expr_selects_requires_claude(markexpr: str) -> bool:
        """Return True only when the expression would select a test carrying
        *solely* the ``requires_claude`` marker (i.e. a genuine opt-in)."""
        return _MarkExpression.compile(markexpr).evaluate(lambda name: name == "requires_claude")

except Exception:  # pragma: no cover — old pytest / import failure

    def _expr_selects_requires_claude(markexpr: str) -> bool:  # type: ignore[misc]
        return markexpr.strip() == "requires_claude"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    markexpr = config.getoption("-m") or ""
    if markexpr and _expr_selects_requires_claude(markexpr):
        return  # explicit opt-in; let them run.
    skip_marker = pytest.mark.skip(
        reason="requires real authenticated `claude` CLI; run with `-m requires_claude`"
    )
    for item in items:
        if "requires_claude" in item.keywords:
            item.add_marker(skip_marker)
