"""Timer service operations whose effects belong to the accessing caller's fiber."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable, Generator
from concurrent.futures import Future
from datetime import timedelta
from types import NoneType
from typing import TypeVar

from pykardia.core.context import Context
from pykardia.core.effect import EffectHandle, EffectScope
from pykardia.core.plugin import Plugin
from pykardia.core.runtime import Completion
from pykardia.core.service import Service, Svc
from pykardia.timer.schedule import Scheduled, abort, spawn
from pykardia.timer.ticks import Disposed, Ticks, TickState

A = TypeVar("A")


class Timer(Service):
    """The timer coeffect; schedules are owned by its caller rather than this value."""

    NAME = "timer"


class TimerPlugin(Plugin[None]):
    """Provide the timer service with unit configuration."""

    NAME = "timer"
    Config = NoneType

    @staticmethod
    def provide() -> list[str]:
        """Declare the timer provision for dependency tooling."""
        return [Timer.NAME]

    @staticmethod
    async def apply(ctx: Context, config: None) -> None:
        """Register the actual timer service on its providing plugin episode."""
        ctx.provide(Timer())


class TimerExt(Svc[Timer]):
    """The scheduling extension of an explicitly accessed timer service handle."""

    def __init__(self, service: Svc[Timer]) -> None:
        """Keep the original handle's shared value, caller and binding attribution."""
        super().__init__(
            service.arc(), service.caller(), service.key(), service.impl_id(), service._provider
        )

    def timeout(self, delay: timedelta, callback: Callable[[], None]) -> EffectHandle:
        """Invoke once after delay, removing the spent effect before calling user code."""
        seconds = duration(delay)
        handle: EffectHandle | None = None

        async def run() -> None:
            """Wait, dispose the spent registration, then invoke the callback in the same turn."""
            await asyncio.sleep(seconds)
            if handle is not None:
                handle.dispose()
            callback()

        def setup(scope: EffectScope) -> None:
            """Retain cancellation as the timer's inverse."""
            task = spawn(run())
            scope.defer_fn(lambda: abort(task))

        handle = self.caller().effect_sync("ctx.timeout()", setup)
        return handle

    def sleep(self, delay: timedelta) -> Awaitable[None]:
        """Register now; resolve after delay or raise Disposed when the caller is released."""
        seconds = duration(delay)
        result: Future[None] = Future()
        lock = threading.Lock()

        async def run() -> None:
            """Resolve once when the sleep duration has elapsed."""
            await asyncio.sleep(seconds)
            with lock:
                if not result.done():
                    result.set_result(None)

        def setup(scope: EffectScope) -> None:
            """Collect cancellation and a failure notification for an interrupted sleeper."""
            task = spawn(run())

            def close() -> None:
                """Stop the task and notify the sleep observer if time has not elapsed."""
                abort(task)
                with lock:
                    if not result.done():
                        result.set_exception(Disposed())

            scope.defer_fn(close)

        try:
            handle = self.caller().effect_sync("ctx.timeout()", setup)
        except Exception as error:
            return Completion.failed(error)
        return Sleep(Completion(result), handle)

    def interval(self, delay: timedelta, callback: Callable[[], None]) -> EffectHandle:
        """Run periodically after the first full delay; missed ticks catch up in bursts."""
        seconds = duration(delay)

        def setup(scope: EffectScope) -> None:
            """Collect the interval task on the caller's accumulator."""
            task = spawn(periodic(seconds, callback))
            scope.defer_fn(lambda: abort(task))

        return self.caller().effect_sync("ctx.interval()", setup)

    def ticks(self, delay: timedelta) -> Ticks:
        """Create an unbuffered interval reader with deterministic explicit close support."""
        seconds = duration(delay)
        state = TickState()

        def setup(scope: EffectScope) -> None:
            """Own periodic notifications independently of the reader object's lifetime."""
            task = spawn(periodic(seconds, state.notify))

            def close() -> None:
                """Cancel ticking and wake every current reader with Disposed."""
                abort(task)
                state.close()

            scope.defer_fn(close)

        handle = self.caller().effect_sync("ctx.interval()", setup)
        return Ticks(state, handle)

    def throttle(
        self, delay: timedelta, trailing: bool, callback: Callable[[A], None]
    ) -> Scheduled[A]:
        """Call immediately outside the window and optionally keep the latest trailing arguments."""
        seconds = duration(delay)
        last_call: float | None = None
        lock = threading.Lock()

        def execute(args: A) -> None:
            """Record the actual callback time before allowing callback reentry."""
            nonlocal last_call
            with lock:
                last_call = asyncio.get_running_loop().time()
            callback(args)

        async def later(remaining: float, args: A) -> None:
            """Wait only the remainder of the current throttling window."""
            await asyncio.sleep(remaining)
            execute(args)

        def trigger(args: A) -> asyncio.Task[None] | None:
            """Choose immediate execution or a replaceable trailing task."""
            now = asyncio.get_running_loop().time()
            with lock:
                elapsed = None if last_call is None else now - last_call
            if elapsed is not None and elapsed < seconds:
                return spawn(later(seconds - elapsed, args)) if trailing else None
            execute(args)
            return None

        return Scheduled(self.caller(), "ctx.throttle()", trigger)

    def debounce(self, delay: timedelta, callback: Callable[[A], None]) -> Scheduled[A]:
        """Invoke only after delay passes without another call, retaining the latest arguments."""
        seconds = duration(delay)

        async def later(args: A) -> None:
            """Run a callback only if its replaceable timer is allowed to finish."""
            await asyncio.sleep(seconds)
            callback(args)

        return Scheduled(self.caller(), "ctx.debounce()", lambda args: spawn(later(args)))


class Sleep:
    """A lazy observer whose cancellation does not dispose a still-owned sleep effect."""

    def __init__(self, result: Completion[None], handle: EffectHandle) -> None:
        """Keep result observation separate from the caller's timer effect ownership."""
        self._result = result
        self._handle = handle

    async def wait(self) -> None:
        """Dispose the spent effect after observing completion or caller-driven disposal."""
        try:
            await self._result
        except Disposed:
            self._handle.dispose()
            raise
        self._handle.dispose()

    def __await__(self) -> Generator[object, None, None]:
        """Create the waiting coroutine only when the observer is actually awaited."""
        return self.wait().__await__()


def duration(delay: timedelta) -> float:
    """Convert an explicit duration at the API boundary, rejecting Rust-impossible negatives."""
    seconds = delay.total_seconds()
    if seconds < 0:
        raise ValueError("timer delay must be nonnegative")
    return seconds


async def periodic(seconds: float, callback: Callable[[], None]) -> None:
    """Use absolute deadlines to match Tokio's default burst behavior for missed ticks."""
    if seconds == 0:
        raise ValueError("period must be non-zero")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while True:
        await asyncio.sleep(max(0.0, deadline - loop.time()))
        deadline += seconds
        callback()
