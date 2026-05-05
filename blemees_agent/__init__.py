"""blemees-agent — Headless agent daemon.

See README.md at the repository root for the wire protocol and architecture.
"""

from importlib import metadata as _metadata

try:
    __version__ = _metadata.version("blemees-agent")
except _metadata.PackageNotFoundError:
    # Running from a source tree without an install. Rare, but `--version`
    # shouldn't crash; emit a sentinel that's obviously not a real release.
    __version__ = "0.0.0+unknown"

PROTOCOL_VERSION = "blemees/2"
