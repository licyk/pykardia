"""Owned asynchronous work and atomic state segments for one root runtime."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Coroutine, Generator
from concurrent.futures import Future
from typing import Generic, TypeVar

T = TypeVar("T")
Action = Callable[[], None]


class Completion(Generic[T]):
    """Observe work without transferring cancellation or ownership to its waiter."""

    def __init__(self, future: Future[T], *, synchronous: bool = False) -> None:
        """Retain the shared result and whether the work completed synchronously."""
        self._future = future
        self._synchronous = synchronous

    @classmethod
    def ready(cls, value: T) -> Completion[T]:
        """Construct a result that is ready before returning to the caller."""
        future: Future[T] = Future()
        future.set_result(value)
        return cls(future, synchronous=True)

    @classmethod
    def failed(cls, error: BaseException) -> Completion[T]:
        """Construct a synchronous failure with the original exception identity."""
        future: Future[T] = Future()
        future.set_exception(error)
        return cls(future, synchronous=True)

    def is_ready(self) -> bool:
        """Return whether the operation finished synchronously, as Rust Disposal does."""
        return self._synchronous

    def result(self) -> T:
        """Retrieve an already-completed result for a synchronous state transition."""
        return self._future.result()

    async def wait(self) -> T:
        """Wait for the shared result while shielding work from waiter cancellation."""
        if self._future.done():
            return self._future.result()
        observer = asyncio.wrap_future(self._future)
        observer.add_done_callback(_observe_failure)
        # wait() preserves ownership without shield() reporting an already-observed error.
        await asyncio.wait([observer])
        return observer.result()

    def __await__(self) -> Generator[object, None, T]:
        """Allow repeated and concurrent await without consuming the result."""
        return self.wait().__await__()


class Runtime:
    """Serialize state segments and keep transitions on a bound asyncio loop.

    State operations passed to run/read must not await or call user callbacks.
    Run returns actions separately so callbacks execute after the state lock is released.
    The loop may be bound explicitly or by the first spawn on a running loop.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Create a root driver, optionally bound to its execution loop."""
        self._loop = loop
        self._lock = threading.Lock()
        self._tasks: set[asyncio.Task[None]] = set()
        self._waiters: set[Future[None]] = set()

    def read(self, operation: Callable[[], T]) -> T:
        """Read root state under its lock without notifying waiters."""
        with self._lock:
            return operation()

    def run(self, operation: Callable[[], tuple[T, list[Action]]]) -> T:
        """Complete one state segment, wake waiters and execute its queued actions."""
        with self._lock:
            result, actions = operation()
            waiters = tuple(self._waiters)
            self._waiters.clear()
        for waiter in waiters:
            waiter.set_result(None)
        for action in actions:
            action()
        return result

    def spawn(self, body: Coroutine[object, object, T]) -> Completion[T]:
        """Start owned work and return a cancellation-independent completion.

        Calls from an external thread require the root to be bound to a loop already.
        The task set is only accessed on that loop; synchronous state access uses run/read.
        """
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        loop = self._loop
        result: Future[T] = Future()

        async def execute() -> None:
            """Transfer the coroutine outcome to the shared completion."""
            try:
                value = await body
            except (Exception, asyncio.CancelledError) as error:
                result.set_exception(error)
            else:
                result.set_result(value)

        def schedule() -> None:
            """Create and retain the task on its owning event loop."""
            task = loop.create_task(execute())
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if current is loop:
            schedule()
        else:
            loop.call_soon_threadsafe(schedule)
        return Completion(result)

    async def wait_until(self, predicate: Callable[[], bool]) -> None:
        """Wait for a state predicate without missing a change between check and wait."""
        while True:
            with self._lock:
                if predicate():
                    return
                future: Future[None] = Future()
                self._waiters.add(future)
            try:
                await Completion(future)
            finally:
                with self._lock:
                    self._waiters.discard(future)


def _observe_failure(future: asyncio.Future[T]) -> None:
    """Retrieve detached observers' failures without consuming the shared result."""
    if not future.cancelled():
        future.exception()
