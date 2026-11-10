"""The threads around a guest process and the state they share.

A writer for its stdin, a reader for its stdout and a tail of its stderr run on threads of
their own, so a call's deadline holds whichever thread waits for it.
"""

from __future__ import annotations

import itertools
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import IO, Protocol

from pykardia.core.json import dumps, loads
from pykardia.core.logger import Level, Logger
from pykardia.core.value import JsonValue
from pykardia.hmr.exit import ExitCause, GuestExit
from pykardia.hmr.guest import abi_level

STDERR_TAIL = 16 * 1024
"""How much of its stderr a guest's exit report keeps."""
EXIT_GRACE = 0.2
"""How long a process that closed its stdout has to exit, and the report waits for stderr."""


class GuestChild(Protocol):
    """The control of a guest process, as ``subprocess.Popen`` has it."""

    @property
    def pid(self) -> int:
        """Return the process id."""
        ...

    def kill(self) -> None:
        """End the process and whatever the spawner started with it; nothing once ended."""
        ...

    def poll(self) -> int | None:
        """Return the return code once the process has ended."""
        ...

    def wait(self, timeout: float | None = None) -> int:
        """Wait for the process to end; called again, return the same code."""
        ...


@dataclass
class Message:
    """A message from the guest for the instance."""

    value: JsonValue


@dataclass
class Cancel:
    """The caller of call ``id`` cancelled it."""

    id: int


@dataclass
class Closed:
    """The guest's stdout closed: on its own, or because it broke the protocol and was killed."""

    reason: str | None


Incoming = Message | Cancel | Closed


class Shared:
    """What the instance, its handle and its process's threads share."""

    def __init__(self, child: GuestChild, stdin: IO[bytes], stderr: bool) -> None:
        """Start the writer thread; the reader threads are started by ``start``."""
        self.child = child
        self.pid: int | None = child.pid
        self.protocol = 1
        self.exit: GuestExit | None = None
        self.incoming: queue.Queue[Incoming] = queue.Queue()
        self._lines: queue.Queue[bytes | None] = queue.Queue()
        self._lock = threading.Lock()
        self._cause: tuple[ExitCause, str | None] | None = None
        self._tail = bytearray()
        self._stderr_closed = threading.Event()
        if not stderr:
            self._stderr_closed.set()
        self._exit_callbacks: list[Callable[[GuestExit], None]] = []
        self._pings: dict[int, Callable[[], None]] = {}
        self._numbers = itertools.count()
        threading.Thread(target=write_lines, args=(stdin, self._lines), daemon=True).start()

    def send(self, message: JsonValue) -> None:
        """Queue a message for the guest's stdin."""
        self._lines.put(dumps(message) + b"\n")

    def mark(self, cause: ExitCause, reason: str | None = None) -> None:
        """Record why the process is ending; the first cause given is the one reported."""
        with self._lock:
            if self._cause is None:
                self._cause = (cause, reason)

    def kill(self, cause: ExitCause, reason: str | None = None) -> None:
        """End the process, recording ``cause`` first."""
        self.mark(cause, reason)
        try:
            self.child.kill()
        except OSError:
            pass

    def reap(self) -> None:
        """Wait for the process to end and publish its exit report, once."""
        if self.exit is not None:
            return
        status = self.settle()
        self._stderr_closed.wait(EXIT_GRACE)
        with self._lock:
            if self.exit is not None:
                return
            cause, reason = self._cause or (ExitCause.EXITED, None)
            self.exit = GuestExit(status, cause, reason, bytes(self._tail))
            callbacks, self._exit_callbacks = self._exit_callbacks, []
            pings, self._pings = list(self._pings.values()), {}
        self._lines.put(None)
        for callback in callbacks:
            callback(self.exit)
        for ping in pings:
            ping()

    def settle(self) -> int | None:
        """Return the exit code, after a grace for a process that is ending anyway."""
        deadline = time.monotonic() + EXIT_GRACE
        while time.monotonic() < deadline:
            status = self.child.poll()
            if status is not None:
                return status
            time.sleep(0.005)
        with self._lock:
            if self._cause is None:
                self._cause = (ExitCause.KILLED, "the guest closed its stdout")
        try:
            self.child.kill()
        except OSError:
            pass
        try:
            return self.child.wait()
        except OSError:
            return None

    def on_exit(self, callback: Callable[[GuestExit], None]) -> None:
        """Run ``callback`` with the report when the process ends, at once if it has."""
        with self._lock:
            exit = self.exit
            if exit is None:
                self._exit_callbacks.append(callback)
                return
        callback(exit)

    def ping(self, answered: Callable[[], None]) -> int:
        """Send a ping; ``answered`` runs on its pong, or when the process ends."""
        number = next(self._numbers)
        with self._lock:
            self._pings[number] = answered
        self.send({"ping": number})
        return number

    def forget_ping(self, number: int) -> None:
        """Drop a ping that was not answered in time."""
        with self._lock:
            self._pings.pop(number, None)

    def pong(self, number: int) -> None:
        """Answer the ping ``number``."""
        with self._lock:
            answered = self._pings.pop(number, None)
        if answered is not None:
            answered()

    def tail(self) -> bytes:
        """Return the stderr tail so far."""
        with self._lock:
            return bytes(self._tail)

    def push_stderr(self, data: bytes) -> None:
        """Keep the last bytes of stderr."""
        with self._lock:
            self._tail += data
            del self._tail[: max(0, len(self._tail) - STDERR_TAIL)]

    def stderr_closed(self) -> None:
        """Mark stderr as fully read."""
        self._stderr_closed.set()


def start(
    child: GuestChild,
    stdin: IO[bytes],
    stdout: IO[bytes],
    stderr: IO[bytes] | None,
    limit: int,
    logger: Logger | None,
) -> Shared:
    """Start the threads of a guest process and return their shared state."""
    shared = Shared(child, stdin, stderr is not None)
    if stderr is not None:
        threading.Thread(target=read_stderr, args=(stderr, shared, logger), daemon=True).start()
    threading.Thread(
        target=read_messages, args=(stdout, limit, shared, logger), daemon=True
    ).start()
    return shared


def write_lines(stdin: IO[bytes], lines: queue.Queue[bytes | None]) -> None:
    """Write queued lines until the process is reaped or the pipe breaks."""
    try:
        while (line := lines.get()) is not None:
            stdin.write(line)
            stdin.flush()
    except (OSError, ValueError):
        pass
    finally:
        try:
            stdin.close()
        except (OSError, ValueError):
            pass


def read_messages(stdout: IO[bytes], limit: int, shared: Shared, logger: Logger | None) -> None:
    """Read one JSON message per line; logs and pongs are handled at once, the rest queued.

    A line that is not JSON, or longer than ``limit``, kills the guest.
    """
    fault: str | None = None
    try:
        while True:
            try:
                line = stdout.readline(limit + 1)
            except (OSError, ValueError):
                break
            if not line:
                break
            if line.endswith(b"\n"):
                line = line[:-1]
            elif len(line) > limit:
                fault = f"a message from the guest exceeds the limit of {limit} bytes"
                break
            try:
                message = loads(line)
                line.decode("utf-8")
            except (ValueError, UnicodeError) as error:
                fault = f"invalid message from guest: {error}"
                break
            obj = message if isinstance(message, dict) else {}
            pong = obj.get("pong")
            if type(pong) is int:
                shared.pong(pong)
                continue
            if obj.get("call") == "log":
                if logger is not None:
                    level = obj.get("level")
                    text = obj.get("text")
                    logger.log(
                        abi_level(level if type(level) is int else 2),
                        text if isinstance(text, str) else "",
                    )
                continue
            shared.incoming.put(Message(message))
    finally:
        if fault is not None:
            shared.kill(ExitCause.KILLED, fault)
        shared.incoming.put(Closed(fault))
        shared.reap()
        try:
            stdout.close()
        except (OSError, ValueError):
            pass


def read_stderr(stderr: IO[bytes], shared: Shared, logger: Logger | None) -> None:
    """Keep the tail of the guest's stderr, logging each line at debug."""
    try:
        while line := stderr.readline(STDERR_TAIL):
            shared.push_stderr(line)
            if logger is not None:
                logger.log(Level.Debug, line.decode("utf-8", errors="replace").rstrip())
    except (OSError, ValueError):
        pass
    finally:
        shared.stderr_closed()
        try:
            stderr.close()
        except (OSError, ValueError):
            pass
