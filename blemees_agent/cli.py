"""Interactive wire-protocol tester for blemeesd.

One REPL command per outbound wire verb (hello, open, send, interrupt,
close, list_sessions, status, session_info, watch/unwatch, ping, raw).
Every inbound frame is echoed back as JSON so you can exercise the
full protocol surface end-to-end without writing a client.

Not a chat UI — no rendering of assistant text, no session management,
no pretty summaries. This is a tester. For a chat experience, use
the `blemees-tui` package, which ships the chat command as `blemees`.

The console script is `blemees-agentctl` (renamed from `blemees` in 0.9.0;
the previous name is now reserved for the chat TUI).

Usage:

    blemees-agentctl                   # connect to default socket, drop to REPL
    blemees-agentctl --socket PATH     # override socket path
    blemees-agentctl --no-connect      # start the REPL without auto-connecting
    blemees-agentctl --version

At the REPL:

    blemees-agentctl> help
    blemees-agentctl> open new backend=claude model=sonnet permission_mode=bypassPermissions
    blemees-agentctl> send <id> hello there
    blemees-agentctl> interrupt <id>
    blemees-agentctl> close <id> --delete
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import shlex
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import PROTOCOL_VERSION, __version__
from .client import default_socket_path

try:
    import readline as _readline
except ImportError:
    _readline = None  # type: ignore[assignment]

PROMPT = "blemees-agentctl> "
_ERASE_LINE = "\r\x1b[2K"
HELP = """\
Commands — each sends one wire frame, responses are printed as they arrive:

  connect [SOCKET]             Open the Unix socket (defaults to
                               $BLEMEES_AGENTD_SOCKET / XDG / /tmp fallback)
  disconnect                   Close the socket

  hello                        (Re)send agent.hello
  ping                         agent.ping  (expect agent.pong)
  status                       agent.status
  sessions <cwd>               agent.list_sessions cwd=<cwd>
  session-info <id>            agent.session_info session_id=<id>

  open <id|new> [k=v ...]      agent.open session_id=<id> …
                               id 'new' generates a uuid
                               backend=<name> picks the agent (claude|codex);
                               default claude. All other k=v go under
                               options.<backend>.* (use options.foo=bar to be
                               explicit). Values coerce true/false/int/json.
  resume <id> [k=v ...]        open with resume=true (shortcut)
  close <id> [--delete]        agent.close session_id=<id> delete=…
  interrupt <id>               agent.interrupt session_id=<id>

  watch <id> [last_seen_seq=N] agent.watch (observer mode)
  unwatch <id>                 agent.unwatch

  send <id> <text...>          agent.user with message={role,user,content:text}
  send-json <id> <json>        agent.user with message=<json>
  raw <json>                   send an arbitrary frame

  pretty on|off                pretty-print inbound JSON (default off)
  quiet on|off                 suppress agent.delta spam (default off)

  help                         this
  quit | exit | .q             leave the REPL
"""


def _ts() -> str:
    return datetime.now(UTC).strftime("%H:%M:%S.%f")[:-3]


def _coerce(v: str) -> Any:
    if v == "null":
        return None
    if v in ("true", "false"):
        return v == "true"
    try:
        if v.isdigit() or (v.startswith("-") and v[1:].isdigit()):
            return int(v)
    except ValueError:
        pass
    try:
        return json.loads(v)
    except (json.JSONDecodeError, ValueError):
        return v


def parse_fields(tokens: list[str]) -> dict[str, Any]:
    """Parse k=v tokens into a dict. Value is coerced to bool/int/JSON if it looks like one.

    Any token without '=' is rejected — callers should peel positional
    args off first.
    """
    out: dict[str, Any] = {}
    for tok in tokens:
        if "=" not in tok:
            raise ValueError(f"expected key=value, got {tok!r}")
        k, v = tok.split("=", 1)
        out[k] = _coerce(v)
    return out


class Harness:
    """Owns the socket and the reader task, exposes one coroutine per verb."""

    def __init__(self, *, pretty: bool = False, quiet: bool = False) -> None:
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.reader_task: asyncio.Task | None = None
        self.pretty = pretty
        self.quiet = quiet
        self._io_lock = (
            asyncio.Lock()
        )  # serialize stdout so reader + sender don't interleave mid-line
        # True from just before `input()` is scheduled until just after
        # it returns. When True, async prints erase the current line so
        # an inbound frame doesn't land glued to the prompt. We don't
        # try to redraw the prompt + readline's line buffer ourselves:
        # macOS Python links against libedit (not GNU readline), where
        # `pre_input_hook` never fires and `get_line_buffer()` keeps the
        # previously-submitted line until readline starts a new read,
        # so any redraw that uses it echoes stale text. Letting readline
        # redisplay the prompt + buffer on the next keystroke is correct
        # everywhere.
        self._prompt_active: bool = False

    # ---- printing -------------------------------------------------

    def _emit(self, text: str) -> None:
        """Write *text* to stdout, erasing the prompt line if active.

        Caller must hold ``_io_lock``. ``text`` should NOT end with a
        trailing newline — this helper supplies one. Readline redraws
        the prompt + in-progress buffer on the next keystroke; until
        then the user sees only the inbound frame on its own line.
        """
        if self._prompt_active:
            sys.stdout.write(_ERASE_LINE)
        sys.stdout.write(text + "\n")
        sys.stdout.flush()

    async def _print_frame(self, direction: str, frame: dict[str, Any]) -> None:
        arrow = "\x1b[36m→\x1b[0m" if direction == "out" else "\x1b[32m←\x1b[0m"
        body = (
            json.dumps(frame, ensure_ascii=False, indent=2)
            if self.pretty
            else json.dumps(frame, ensure_ascii=False)
        )
        header = f"{arrow} {_ts()}"
        if direction == "in":
            t = frame.get("type", "?")
            seq = frame.get("seq")
            header += f" {t}" + (f" seq={seq}" if seq is not None else "")
        async with self._io_lock:
            self._emit(f"{header}  {body}")

    async def _print_note(self, msg: str) -> None:
        async with self._io_lock:
            self._emit(f"\x1b[33m· {msg}\x1b[0m")

    # ---- send/recv ------------------------------------------------

    async def _send(self, frame: dict[str, Any]) -> None:
        if self.writer is None:
            raise RuntimeError("not connected — run `connect` first")
        data = (json.dumps(frame, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        self.writer.write(data)
        await self.writer.drain()
        await self._print_frame("out", frame)

    async def _reader_loop(self) -> None:
        assert self.reader is not None
        try:
            while True:
                raw = await self.reader.readuntil(b"\n")
                try:
                    evt = json.loads(raw.rstrip(b"\r\n").decode("utf-8"))
                except json.JSONDecodeError:
                    await self._print_note(f"non-JSON line from daemon: {raw!r}")
                    continue
                if self.quiet and evt.get("type") == "agent.delta":
                    continue
                await self._print_frame("in", evt)
        except asyncio.CancelledError:
            raise
        except (asyncio.IncompleteReadError, ConnectionError, OSError) as e:
            await self._print_note(f"connection closed: {e.__class__.__name__}")
            self.reader = None
            self.writer = None

    # ---- commands -------------------------------------------------

    async def connect(self, path: str | None) -> None:
        if self.writer is not None:
            await self._print_note("already connected; `disconnect` first")
            return
        path = path or default_socket_path()
        self.reader, self.writer = await asyncio.open_unix_connection(path)
        self.reader_task = asyncio.create_task(self._reader_loop())
        await self._print_note(f"connected: {path}")
        await self.hello()

    async def disconnect(self) -> None:
        if self.writer is None:
            return
        self.writer.close()
        with contextlib.suppress(Exception):
            await self.writer.wait_closed()
        if self.reader_task is not None:
            self.reader_task.cancel()
            with contextlib.suppress(BaseException):
                await self.reader_task
            self.reader_task = None
        self.reader = self.writer = None
        await self._print_note("disconnected")

    async def hello(self) -> None:
        await self._send(
            {
                "type": "agent.hello",
                "client": f"blemees-agentctl/{__version__}",
                "protocol": PROTOCOL_VERSION,
            }
        )

    async def ping(self) -> None:
        await self._send({"type": "agent.ping", "id": _req_id()})

    async def status(self) -> None:
        await self._send({"type": "agent.status", "id": _req_id()})

    async def list_sessions(self, cwd: str) -> None:
        await self._send({"type": "agent.list_sessions", "id": _req_id(), "cwd": cwd})

    async def session_info(self, session_id: str) -> None:
        await self._send(
            {"type": "agent.session_info", "id": _req_id(), "session_id": session_id}
        )

    async def open(self, session_id: str, fields: dict[str, Any]) -> str:
        if session_id == "new":
            session_id = str(uuid.uuid4())
        # Pull the well-known top-level keys; anything else goes under
        # options.<backend>.*. Lets you write `open new backend=claude
        # model=sonnet` instead of having to spell out the nesting.
        backend = fields.pop("backend", "claude")
        resume = fields.pop("resume", False)
        last_seen_seq = fields.pop("last_seen_seq", None)
        frame: dict[str, Any] = {
            "type": "agent.open",
            "id": _req_id(),
            "session_id": session_id,
            "backend": backend,
            "options": {backend: fields},
        }
        if resume:
            frame["resume"] = True
        if last_seen_seq is not None:
            frame["last_seen_seq"] = last_seen_seq
        await self._send(frame)
        return session_id

    async def close_session(self, session_id: str, delete: bool) -> None:
        await self._send({"type": "agent.close", "session_id": session_id, "delete": delete})

    async def interrupt(self, session_id: str) -> None:
        await self._send({"type": "agent.interrupt", "session_id": session_id})

    async def watch(self, session_id: str, fields: dict[str, Any]) -> None:
        frame: dict[str, Any] = {
            "type": "agent.watch",
            "id": _req_id(),
            "session_id": session_id,
        }
        frame.update(fields)
        await self._send(frame)

    async def unwatch(self, session_id: str) -> None:
        await self._send({"type": "agent.unwatch", "id": _req_id(), "session_id": session_id})

    async def send_user(self, session_id: str, text: str) -> None:
        await self._send(
            {
                "type": "agent.user",
                "session_id": session_id,
                "message": {"role": "user", "content": text},
            }
        )

    async def send_user_raw(self, session_id: str, message_json: str) -> None:
        msg = json.loads(message_json)
        await self._send({"type": "agent.user", "session_id": session_id, "message": msg})

    async def raw(self, payload: str) -> None:
        frame = json.loads(payload)
        await self._send(frame)


def _req_id() -> str:
    return f"req_{uuid.uuid4().hex[:8]}"


async def dispatch(h: Harness, line: str) -> bool:
    """Run one REPL command. Return False to signal quit."""
    # Split off the command word and the remainder; shlex-tokenize for most
    # commands but keep the raw remainder for JSON payloads.
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return True
    head, _, rest = stripped.partition(" ")
    rest = rest.strip()
    cmd = head.lower()

    if cmd in ("quit", "exit", ".q"):
        return False

    if cmd in ("help", "?"):
        print(HELP)
        return True

    if cmd == "connect":
        await h.connect(rest or None)
        return True
    if cmd == "disconnect":
        await h.disconnect()
        return True
    if cmd == "hello":
        await h.hello()
        return True
    if cmd == "ping":
        await h.ping()
        return True
    if cmd == "status":
        await h.status()
        return True

    if cmd == "sessions":
        if not rest:
            print("usage: sessions <cwd>")
            return True
        await h.list_sessions(rest)
        return True
    if cmd == "session-info":
        if not rest:
            print("usage: session-info <session_id>")
            return True
        await h.session_info(rest)
        return True

    if cmd in ("open", "resume"):
        tokens = shlex.split(rest)
        if not tokens:
            print(f"usage: {cmd} <id|new> [k=v ...]")
            return True
        sid, *fields = tokens
        try:
            parsed = parse_fields(fields)
        except ValueError as e:
            print(f"error: {e}")
            return True
        if cmd == "resume":
            parsed.setdefault("resume", True)
        resolved = await h.open(sid, parsed)
        if sid == "new":
            await h._print_note(f"session_id: {resolved}")
        return True

    if cmd == "close":
        tokens = shlex.split(rest)
        if not tokens:
            print("usage: close <id> [--delete]")
            return True
        sid = tokens[0]
        delete = "--delete" in tokens[1:]
        await h.close_session(sid, delete=delete)
        return True

    if cmd == "interrupt":
        if not rest:
            print("usage: interrupt <id>")
            return True
        await h.interrupt(rest.split()[0])
        return True

    if cmd == "watch":
        tokens = shlex.split(rest)
        if not tokens:
            print("usage: watch <id> [last_seen_seq=N]")
            return True
        sid, *fields = tokens
        try:
            parsed = parse_fields(fields)
        except ValueError as e:
            print(f"error: {e}")
            return True
        await h.watch(sid, parsed)
        return True

    if cmd == "unwatch":
        if not rest:
            print("usage: unwatch <id>")
            return True
        await h.unwatch(rest.split()[0])
        return True

    if cmd == "send":
        sid, _, text = rest.partition(" ")
        if not sid or not text:
            print("usage: send <id> <text...>")
            return True
        await h.send_user(sid, text)
        return True

    if cmd == "send-json":
        sid, _, payload = rest.partition(" ")
        if not sid or not payload:
            print("usage: send-json <id> <json>")
            return True
        try:
            await h.send_user_raw(sid, payload)
        except json.JSONDecodeError as e:
            print(f"invalid JSON: {e}")
        return True

    if cmd == "raw":
        if not rest:
            print("usage: raw <json>")
            return True
        try:
            await h.raw(rest)
        except json.JSONDecodeError as e:
            print(f"invalid JSON: {e}")
        return True

    if cmd == "pretty":
        h.pretty = _on_off(rest)
        await h._print_note(f"pretty={h.pretty}")
        return True
    if cmd == "quiet":
        h.quiet = _on_off(rest)
        await h._print_note(f"quiet={h.quiet}")
        return True

    print(f"unknown command: {cmd!r}. `help` for a list.")
    return True


def _on_off(s: str) -> bool:
    v = s.strip().lower()
    if v in ("", "on", "true", "1", "yes"):
        return True
    return False


async def repl(initial_connect: bool, socket_path: str | None) -> int:
    # Readline buys us up-arrow history + line editing on posix; the
    # module-level import lets _emit() reach into the line buffer to
    # redraw the prompt around async inbound frames.
    histfile: Path | None = None
    harness = Harness()
    if _readline is not None:
        # History stays at ~/.blemees_history so existing users don't
        # lose their command history across the rename.
        histfile = Path.home() / ".blemees_history"
        with contextlib.suppress(FileNotFoundError, OSError):
            _readline.read_history_file(str(histfile))

    print("blemees-agentctl — interactive wire tester. Type `help` for commands; Ctrl-D to quit.")

    if initial_connect:
        try:
            await harness.connect(socket_path)
        except (ConnectionError, OSError, FileNotFoundError) as e:
            print(f"connect failed: {e}. Try `connect <path>`.")

    try:
        while True:
            harness._prompt_active = True
            try:
                line = await asyncio.to_thread(input, PROMPT)
            except EOFError:
                harness._prompt_active = False
                print()
                break
            except KeyboardInterrupt:
                harness._prompt_active = False
                print()
                continue
            harness._prompt_active = False
            try:
                keep_going = await dispatch(harness, line)
            except Exception as e:  # noqa: BLE001 — one bad command shouldn't crash the REPL
                print(f"error: {e.__class__.__name__}: {e}")
                continue
            if not keep_going:
                break
    finally:
        with contextlib.suppress(Exception):
            await harness.disconnect()
        if _readline is not None and histfile is not None:
            with contextlib.suppress(OSError):
                _readline.write_history_file(str(histfile))

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="blemees-agentctl",
        description="Interactive wire-protocol tester for blemeesd.",
    )
    parser.add_argument("--socket", help="Path to the blemeesd Unix socket")
    parser.add_argument(
        "--no-connect",
        action="store_true",
        help="Start the REPL without auto-connecting",
    )
    parser.add_argument("--version", action="store_true")
    args = parser.parse_args(argv)

    if args.version:
        print(f"blemees-agentctl {__version__}")
        return 0

    try:
        return asyncio.run(repl(not args.no_connect, args.socket))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
