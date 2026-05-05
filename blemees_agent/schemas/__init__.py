"""JSON Schemas for the `blemees/1` wire protocol.

Every frame on the wire has a corresponding Draft 2020-12 schema. This
subpackage ships with the wheel so clients in any environment can
validate their implementations without copying the JSON out of the
source tree.

    from blemees_agent.schemas import load
    hello = load("inbound/blemeesd.hello.json")

    # Full registry with $ref resolution (needs `jsonschema` + `referencing`):
    from blemees_agent.schemas import iter_schemas
    from referencing import Registry, Resource
    reg = Registry().with_resources(
        (s["$id"], Resource.from_contents(s)) for s in iter_schemas()
    )

The `files()` helper returns the package as an `importlib.resources`
`Traversable` if you need direct path access (e.g. for tooling that
expects on-disk JSON paths via `as_file()`).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from importlib import resources
from typing import Any

__all__ = ["files", "load", "iter_schemas"]


def files() -> resources.abc.Traversable:
    """Return a `Traversable` rooted at this subpackage.

    Use `/` or `joinpath()` to drill into `inbound/` or `outbound/`,
    and `as_file()` if a downstream tool needs an on-disk path.
    """
    return resources.files(__name__)


def load(name: str) -> dict[str, Any]:
    """Load one schema by its package-relative path.

    Example: ``load("inbound/blemeesd.hello.json")``.
    """
    return json.loads((files() / name).read_text(encoding="utf-8"))


def iter_schemas() -> Iterator[dict[str, Any]]:
    """Yield every shipped schema as a parsed dict.

    Order is unspecified. Useful for building a `referencing.Registry`
    that resolves cross-schema `$ref`s without per-file path knowledge.
    """
    root = files()
    for direction in ("inbound", "outbound"):
        for entry in (root / direction).iterdir():
            if entry.name.endswith(".json"):
                yield json.loads(entry.read_text(encoding="utf-8"))
    common = root / "_common.json"
    if common.is_file():
        yield json.loads(common.read_text(encoding="utf-8"))
