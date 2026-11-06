"""Unbuffered interval notifications and deterministic tick-reader cleanup."""

from __future__ import annotations

import threading
from concurrent.futures import Future
from types import TracebackType

from pykardia.core.effect import EffectHandle
from pykardia.core.runtime import Completion


class Disposed(Exception):
    """A timer was disposed while a caller was waiting for it."""

    def __init__(self) -> None:
        """Describe the closed timer using the reference diagnostic."""
        super().__init__("Context has been disposed")


class TickState:
    """Notify current readers only; missed interval ticks are never queued."""

    def __init__(self) -> None:
        """Create an open notification source with no buffered tick."""
        self._lock = threading.Lock()
        self._closed = False
        self._waiters: set[Future[None]] = set()

    def notify(self) -> None:
        """Deliver one tick to all readers that are already waiting."""
        with self._lock:
            waiters = tuple(self._waiters)
            self._waiters.clear()
        for waiter in waiters:
            waiter.set_result(None)

    def close(self) -> None:
        """Mark the source closed and wake all readers so they fail with Disposed."""
        with self._lock:
            self._closed = True
        self.notify()

    async def tick(self) -> None:
        """Wait for the next notification and fail if disposal wins before resumption."""
        future: Future[None] = Future()
        with self._lock:
            if self._closed:
                raise Disposed()
            self._waiters.add(future)
        try:
            await Completion(future)
        finally:
            with self._lock:
                self._waiters.discard(future)
        with self._lock:
            if self._closed:
                raise Disposed()


class Ticks:
    """Read interval ticks; close or leave a context manager to dispose deterministically.

    Destruction also disposes the interval, but Python does not guarantee when an
    object retained by a reference cycle is destroyed. Use close for that guarantee.
    """

    def __init__(self, state: TickState, handle: EffectHandle) -> None:
        """Associate a notification source with its owning interval effect."""
        self._state: TickState = state
        self._handle: EffectHandle = handle

    async def tick(self) -> None:
        """Wait for the next tick, raising Disposed after the interval is released."""
        await self._state.tick()

    def close(self) -> None:
        """Immediately dispose the interval at most once."""
        self._handle.dispose()

    def __enter__(self) -> Ticks:
        """Return this reader for a deterministic resource scope."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Dispose the interval on normal or exceptional scope exit."""
        self.close()

    def __del__(self) -> None:
        """Release the interval when the interpreter actually destroys this reader."""
        self.close()
