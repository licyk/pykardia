"""Reversible actions with a synchronous prefix and sequential LIFO cleanup."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Generator
from typing import Protocol

from pykardia.core.runtime import Completion, Runtime


class Disposal:
    """Observe cleanup that has already started and proceeds without its observer."""

    def __init__(self, completion: Completion[None]) -> None:
        """Wrap the runtime-owned result of a cleanup sequence."""
        self._completion = completion
        self._consumed = False

    def is_ready(self) -> bool:
        """Return whether cleanup finished synchronously, even after async completion."""
        return self._completion.is_ready()

    def _take_ready(self) -> None:
        """Consume a synchronous result, matching Disposal::Ready's one-shot result."""
        if not self._consumed:
            self._consumed = True
            self._completion.result()

    async def wait(self) -> None:
        """Observe cleanup without propagating waiter cancellation into its task."""
        if self.is_ready():
            self._take_ready()
        else:
            await self._completion

    def __await__(self) -> Generator[object, None, None]:
        """Wait for the cleanup's result."""
        return self.wait().__await__()


class Disposable(Protocol):
    """An effect handle whose inverse can join another cleanup sequence."""

    def dispose(self) -> Disposal:
        """Start cleanup and return its completion."""
        ...


class Disposer:
    """An inverse consumed at most once; its callback runs when disposal starts."""

    def __init__(self, action: Callable[[], Awaitable[None] | None]) -> None:
        """Retain a callback that may finish immediately or return asynchronous work."""
        self._action: Callable[[], Awaitable[None] | None] | None = action

    @classmethod
    def new(cls, action: Callable[[], None]) -> Disposer:
        """Create a synchronous inverse; exceptions become cleanup failures."""
        return cls(action)

    fallible = new

    @classmethod
    def future(cls, action: Callable[[], Awaitable[None]]) -> Disposer:
        """Create an asynchronous inverse with a synchronously invoked factory."""
        return cls(action)

    try_future = future

    @classmethod
    def from_handle(cls, handle: Disposable) -> Disposer:
        """Adopt a nested handle without turning synchronous cleanup asynchronous."""
        return cls(handle.dispose)

    def call(self) -> Awaitable[None] | None:
        """Consume the inverse and return only its unfinished asynchronous portion."""
        action, self._action = self._action, None
        if action is None:
            return None
        result = action()
        if isinstance(result, Disposal) and result.is_ready():
            result._take_ready()
            return None
        return result


def run_sequence(runtime: Runtime, disposers: list[Disposer]) -> Disposal:
    """Run inverses newest first, continuing after failures and reporting the first.

    Synchronous callbacks run before this function returns. From the first awaitable
    onward, the runtime owns a task which runs the remainder strictly in sequence.
    """
    rest = list(disposers)
    first_error: BaseException | None = None
    while rest:
        try:
            pending = rest.pop().call()
        except (Exception, asyncio.CancelledError) as error:
            if first_error is None:
                first_error = error
            continue
        if pending is not None:
            return Disposal(runtime.spawn(_finish_sequence(pending, rest, first_error)))
    return Disposal(
        Completion.ready(None) if first_error is None else Completion.failed(first_error)
    )


async def _finish_sequence(
    pending: Awaitable[None], rest: list[Disposer], first_error: BaseException | None
) -> None:
    """Finish the first pending inverse, then run all remaining inverses in order."""
    try:
        await pending
    except (Exception, asyncio.CancelledError) as error:
        if first_error is None:
            first_error = error
    while rest:
        try:
            following = rest.pop().call()
            if following is not None:
                await following
        except (Exception, asyncio.CancelledError) as error:
            if first_error is None:
                first_error = error
    if first_error is not None:
        raise first_error
