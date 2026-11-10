"""The handle of a running process guest, its exit report and the bridge's events."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from pykardia.core.events import Event
from pykardia.core.fiber import Fiber

if TYPE_CHECKING:
    from pykardia.hmr.pipes import Shared


class ExitCause(Enum):
    """How a guest process ended."""

    EXITED = "exited"
    """It exited on its own."""
    KILLED = "killed"
    """It was killed: a missed deadline, a protocol violation, or its host."""
    DISPOSED = "disposed"
    """Its fiber disposed it."""


@dataclass(frozen=True)
class GuestExit:
    """The report of a guest process that ended."""

    status: int | None
    """The return code (negative for a signal), or ``None`` if waiting failed."""
    cause: ExitCause
    reason: str | None
    """Why it was killed, for ``ExitCause.KILLED``."""
    stderr_tail: bytes
    """The last 16 KiB the process wrote to stderr."""

    def __str__(self) -> str:
        """Describe the exit as the fiber's error reports it."""
        if self.cause is ExitCause.EXITED:
            text = "the guest process exited"
        elif self.cause is ExitCause.KILLED:
            text = f"the guest process was killed: {self.reason}"
        else:
            text = "the guest process was disposed"
        return text if self.status is None else f"{text} (exit status: {self.status})"


class GuestHandle:
    """A running process guest, for its host: its process, its exit and a liveness check."""

    def __init__(self, shared: Shared) -> None:
        """Wrap the state shared with the process's threads."""
        self._shared = shared

    def id(self) -> int | None:
        """Return the process id the spawner reported."""
        return self._shared.pid

    def protocol(self) -> int:
        """Return the protocol the guest speaks: 1 or 2 (``kardia-guest/2``)."""
        return self._shared.protocol

    def exit(self) -> GuestExit | None:
        """Return the exit report, once the process has ended."""
        return self._shared.exit

    def on_exit(self, callback: Callable[[GuestExit], None]) -> None:
        """Run ``callback`` with the report when the process ends, at once if it has."""
        self._shared.on_exit(callback)

    async def exited(self) -> GuestExit:
        """Wait for the exit report."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[GuestExit] = loop.create_future()

        def resolve(exit: GuestExit) -> None:
            """Hand the report to the waiting loop."""
            loop.call_soon_threadsafe(lambda: future.done() or future.set_result(exit))

        self.on_exit(resolve)
        return await future

    def stderr_tail(self) -> bytes:
        """Return the last 16 KiB the process wrote to stderr so far."""
        return self._shared.tail()

    def kill(self) -> None:
        """Kill the process; its report says it was killed by the host."""
        self._shared.kill(ExitCause.KILLED, "killed by the host")

    async def ping(self, timeout: float) -> float:
        """Check that a ``kardia-guest/2`` guest answers, even during a call; return the RTT."""
        if self.protocol() < 2:
            raise ValueError("ping needs a guest speaking kardia-guest/2")
        if self.exit() is not None:
            raise ValueError("the guest process exited")
        loop = asyncio.get_running_loop()
        pong: asyncio.Future[None] = loop.create_future()
        start = time.monotonic()

        def answered() -> None:
            """Resolve the pong on the waiting loop."""
            loop.call_soon_threadsafe(lambda: pong.done() or pong.set_result(None))

        number = self._shared.ping(answered)
        try:
            await asyncio.wait_for(pong, timeout)
        except asyncio.TimeoutError:
            self._shared.forget_ping(number)
            raise ValueError("the guest did not answer the ping in time") from None
        if self.exit() is not None:
            raise ValueError("the guest process exited")
        return time.monotonic() - start


@dataclass
class GuestStarted(Event[None]):
    """Emitted on a guest fiber when its process has started, before ``apply``."""

    fiber: Fiber
    handle: GuestHandle


@dataclass
class GuestExited(Event[None]):
    """Emitted on a guest fiber when its process ends before the fiber disposes it."""

    fiber: Fiber
    exit: GuestExit
