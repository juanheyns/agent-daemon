"""Pin `blemees_agent.__version__` to `pyproject.toml`'s declared version.

This catches the failure mode that bit us once already: a hand-bumped
pyproject diverging from a hardcoded `__version__`, leaving
`blemeesd --version` and the wire-protocol `daemon` field reporting
stale numbers. After the switch to `importlib.metadata.version()`,
this test verifies the lookup is wired up correctly.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import blemees_agent


def test_runtime_version_matches_pyproject():
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    with pyproject.open("rb") as f:
        cfg = tomllib.load(f)
    assert blemees_agent.__version__ == cfg["project"]["version"], (
        f"blemees_agent.__version__={blemees_agent.__version__!r} but pyproject declares "
        f"{cfg['project']['version']!r}. importlib.metadata is reading the "
        "wrong wheel — usually means the editable install is stale; rerun "
        "`uv pip install -e .[dev]`."
    )


def test_runtime_version_is_not_unknown_sentinel():
    # If the package isn't installed at all, __init__.py falls back to a
    # 0.0.0+unknown sentinel. CI runs in an installed environment, so this
    # should never trip — but if it does, the test message will explain why
    # the matching test above is also failing.
    assert blemees_agent.__version__ != "0.0.0+unknown", (
        "blemees is not installed in the test environment; the metadata "
        "lookup fell back to its sentinel. Install with "
        "`uv pip install -e .[dev]` before running tests."
    )
