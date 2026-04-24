"""Per-session event replay primitives.

Every outbound frame the daemon emits for a session carries a monotonic
``seq`` assigned by the :class:`Session`. Two storage layers sit behind
that seq:

* :class:`RingBuffer` — in-memory, bounded, newest-first-drops. Enables
  replay after a brief client disconnect (option 2 in the design).

* :class:`DurableEventLog` — optional append-only JSONL on disk. Lets
  replay survive daemon restarts; seeds the ring buffer on re-open
  (option 3). Opt-in via ``event_log_dir`` config.

Both are single-writer: only the session's event dispatcher appends.
Readers (reattaching connections) get a point-in-time snapshot.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Iterable
from pathlib import Path
from typing import IO


class RingBuffer:
    """Bounded queue of the last N frames, keyed by ``frame["seq"]``."""

    def __init__(self, capacity: int) -> None:
        self._capacity = max(1, capacity)
        self._buf: deque[dict] = deque(maxlen=self._capacity)

    @property
    def capacity(self) -> int:
        return self._capacity

    def append(self, frame: dict) -> None:
        self._buf.append(frame)

    def since(self, seq: int) -> list[dict]:
        """Return frames with ``seq > <seq>``, in order."""
        return [f for f in self._buf if f.get("seq", 0) > seq]

    def earliest_seq(self) -> int | None:
        if not self._buf:
            return None
        return self._buf[0].get("seq")

    def latest_seq(self) -> int | None:
        if not self._buf:
            return None
        return self._buf[-1].get("seq")

    def __len__(self) -> int:
        return len(self._buf)

    def extend(self, frames: Iterable[dict]) -> None:
        for f in frames:
            self._buf.append(f)


class DurableEventLog:
    """Append-only JSONL log for a single session. File handle is lazy."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh: IO | None = None

    def open(self) -> None:
        if self._fh is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Line-buffered so events become durable one line at a time.
        self._fh = self.path.open("a", buffering=1, encoding="utf-8")

    def append(self, frame: dict) -> None:
        if self._fh is None:
            self.open()
        assert self._fh is not None
        self._fh.write(json.dumps(frame, ensure_ascii=False, separators=(",", ":")) + "\n")

    def tail(self, n: int) -> list[dict]:
        """Return the last ``n`` frames from disk (best-effort; skips bad lines)."""
        if not self.path.is_file():
            return []
        try:
            size = self.path.stat().st_size
        except OSError:
            return []
        # Read last ~256 KiB or whole file, whichever is smaller — enough
        # for a few thousand short events, cheap for short sessions.
        read_span = min(size, 256 * 1024)
        with self.path.open("rb") as fh:
            fh.seek(size - read_span)
            chunk = fh.read(read_span)
        lines = chunk.split(b"\n")
        # Drop the first partial line if we didn't start at byte 0.
        if read_span < size and lines:
            lines = lines[1:]
        out: list[dict] = []
        for raw in lines[-n * 2 :]:  # overshoot for bad lines
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(obj, dict):
                out.append(obj)
        return out[-n:]

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None

    def unlink(self) -> None:
        self.close()
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def event_log_path(base_dir: Path | str, session_id: str) -> Path:
    """Return the log file path for a session under ``base_dir``."""
    return Path(base_dir) / f"{session_id}.jsonl"
