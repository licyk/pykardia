"""Timer service parity with the source timer integration cases."""

from __future__ import annotations

import asyncio
import time
import unittest
from datetime import timedelta

import async_timeout

from pykardia import Context, FiberState, Inject
from pykardia.timer import Disposed, Timer, TimerPlugin

DELAY = timedelta(milliseconds=30)


class TimerTests(unittest.IsolatedAsyncioTestCase):
    """Exercise actual task scheduling, service handles and effect disposal."""

    ctx: Context

    async def asyncSetUp(self) -> None:
        """Provide a timer to a separately owned consumer fiber."""
        self.root = Context()
        self.deadline = async_timeout.timeout(5)
        await self.deadline.__aenter__()
        await self.root.plugin(TimerPlugin, None).settled()

        async def consume(ctx: Context) -> None:
            """Retain the use-site context for calls made after plugin application."""
            self.ctx = ctx

        self.fiber = self.root.inject(Inject.of(Timer), consume)
        await self.fiber.settled()
        self.timer = self.ctx.timer()

    async def asyncTearDown(self) -> None:
        """Release every live task before leaving the test event loop."""
        if not self.deadline.expired:
            await self.root.fiber().dispose()
        await self.deadline.__aexit__(None, None, None)

    async def test_timeout_removes_effect_before_callback(self) -> None:
        """A one-shot callback sees its spent registration already removed."""
        done = asyncio.Event()
        counts: list[int] = []

        def callback() -> None:
            """Observe the caller's actual accumulator inside the fired callback."""
            counts.append(self.fiber.effect_count())
            done.set()

        handle = self.timer.timeout(DELAY, callback)
        self.assertEqual(self.fiber.effect_count(), 1)
        await done.wait()
        self.assertEqual(counts, [0])
        self.assertFalse(handle.is_active())

    async def test_timeout_dispose(self) -> None:
        """Disposing before the deadline prevents the callback from running."""
        calls: list[int] = []
        handle = self.timer.timeout(DELAY, lambda: calls.append(1))
        await handle.dispose()
        await asyncio.sleep(DELAY.total_seconds() * 2)
        self.assertEqual(calls, [])
        self.assertEqual(self.fiber.effect_count(), 0)

    async def test_sleep_resolves_or_fails_with_context(self) -> None:
        """Sleep registers synchronously and reports caller disposal to its observer."""
        observer = self.timer.sleep(DELAY)
        self.assertEqual(self.fiber.effect_count(), 1)
        await observer
        self.assertEqual(self.fiber.effect_count(), 0)
        interrupted = self.timer.sleep(timedelta(seconds=30))
        await self.fiber.dispose()
        with self.assertRaisesRegex(Disposed, "Context has been disposed"):
            await interrupted

    async def test_interval_first_tick_is_delayed(self) -> None:
        """Interval callbacks begin after one full delay and stop on explicit disposal."""
        done = asyncio.Event()
        calls: list[int] = []

        def callback() -> None:
            """Stop the actual interval after its second invocation."""
            calls.append(len(calls))
            if len(calls) == 2:
                handle.dispose()
                done.set()

        handle = self.timer.interval(DELAY, callback)
        await asyncio.sleep(0)
        self.assertEqual(calls, [])
        await done.wait()
        self.assertEqual(calls, [0, 1])
        self.assertEqual(self.fiber.effect_count(), 0)

    async def test_ticks_until_closed(self) -> None:
        """Tick readers receive successive notifications and close their interval effect."""
        ticks = self.timer.ticks(DELAY)
        await ticks.tick()
        await ticks.tick()
        self.assertEqual(self.fiber.effect_count(), 1)
        ticks.close()
        self.assertEqual(self.fiber.effect_count(), 0)
        with self.assertRaises(Disposed):
            await ticks.tick()

    async def test_ticks_fail_when_context_disposed(self) -> None:
        """Disposal wakes an already waiting reader with the source diagnostic."""
        ticks = self.timer.ticks(timedelta(seconds=30))
        reader = asyncio.create_task(ticks.tick())
        await asyncio.sleep(0)
        await self.fiber.dispose()
        with self.assertRaises(Disposed):
            await reader

    async def test_ticks_scope_exit_and_exception(self) -> None:
        """Explicit Python resource scopes replace Rust's deterministic Drop on early exit."""
        with self.timer.ticks(DELAY) as ticks:
            await ticks.tick()
        self.assertEqual(self.fiber.effect_count(), 0)
        with self.assertRaisesRegex(ValueError, "stop"):
            with self.timer.ticks(DELAY) as ticks:
                await ticks.tick()
                raise ValueError("stop")
        self.assertEqual(self.fiber.effect_count(), 0)

    async def test_ticks_do_not_buffer_and_broadcast_to_waiters(self) -> None:
        """Past notifications do not satisfy a new reader; current readers share a tick."""
        ticks = self.timer.ticks(DELAY)
        await asyncio.sleep(DELAY.total_seconds() * 2.5)
        first = asyncio.create_task(ticks.tick())
        second = asyncio.create_task(ticks.tick())
        await asyncio.sleep(0)
        self.assertFalse(first.done())
        self.assertFalse(second.done())
        await asyncio.gather(first, second)
        ticks.close()

    async def test_throttle_trailing_keeps_latest_arguments(self) -> None:
        """Calls within a window replace the pending trailing arguments."""
        calls: list[int] = []
        done = asyncio.Event()

        def callback(value: int) -> None:
            """Signal the trailing callback while recording its actual argument."""
            calls.append(value)
            if value == 3:
                done.set()

        schedule = self.timer.throttle(DELAY, True, callback)
        schedule.call(1)
        schedule.call(2)
        schedule.call(3)
        self.assertEqual(calls, [1])
        await done.wait()
        self.assertEqual(calls, [1, 3])
        await schedule.dispose()
        schedule.call(4)
        self.assertEqual(calls, [1, 3])

    async def test_throttle_without_trailing(self) -> None:
        """Suppressed calls remain absent when trailing execution is disabled."""
        calls: list[int] = []
        schedule = self.timer.throttle(DELAY, False, calls.append)
        schedule.call(1)
        schedule.call(2)
        await asyncio.sleep(DELAY.total_seconds() * 2)
        self.assertEqual(calls, [1])
        schedule.call(3)
        self.assertEqual(calls, [1, 3])

    async def test_throttle_stops_with_context_or_disposal(self) -> None:
        """Both explicit disposal and caller retirement cancel pending trailing work."""
        calls: list[int] = []
        first = self.timer.throttle(DELAY, True, calls.append)
        second = self.timer.throttle(DELAY, True, calls.append)
        first.call(1)
        first.call(2)
        second.call(3)
        second.call(4)
        await first.dispose()
        await self.fiber.dispose()
        second.call(5)
        await asyncio.sleep(DELAY.total_seconds() * 2)
        self.assertEqual(calls, [1, 3])

    async def test_debounce_latest_args(self) -> None:
        """A burst produces only its most recent argument after the quiet period."""
        done = asyncio.Event()
        calls: list[str] = []

        def callback(value: str) -> None:
            """Record the single successful invocation."""
            calls.append(value)
            done.set()

        schedule = self.timer.debounce(DELAY, callback)
        schedule.call("a")
        schedule.call("b")
        self.assertEqual(calls, [])
        await done.wait()
        self.assertEqual(calls, ["b"])

    async def test_debounce_stops_with_context(self) -> None:
        """Caller disposal suppresses the pending debounce and all future calls."""
        calls: list[int] = []
        schedule = self.timer.debounce(DELAY, calls.append)
        schedule.call(1)
        await self.fiber.dispose()
        schedule.call(2)
        await asyncio.sleep(DELAY.total_seconds() * 2)
        self.assertEqual(calls, [])

    async def test_zero_delay_and_callback_failure(self) -> None:
        """Zero one-shots run later; interval failures are independent of fiber health."""
        loop = asyncio.get_running_loop()
        failures: list[dict[str, object]] = []
        reported = asyncio.Event()

        def report(loop: asyncio.AbstractEventLoop, context: dict[str, object]) -> None:
            """Capture actual loop-reported task failures without mocking the timer."""
            failures.append(context)
            reported.set()

        loop.set_exception_handler(report)
        try:
            done = asyncio.Event()
            self.timer.timeout(timedelta(), done.set)
            self.assertFalse(done.is_set())
            await done.wait()
            interval = self.timer.interval(timedelta(), lambda: None)
            await reported.wait()
            self.assertEqual(str(failures[0]["exception"]), "period must be non-zero")
            self.assertTrue(interval.is_active())
            self.assertEqual(self.fiber.state(), FiberState.Active)
            await interval.dispose()
            reported.clear()

            def fail() -> None:
                """Raise from the actual scheduled user callback."""
                raise ValueError("callback failed")

            timeout = self.timer.timeout(timedelta(), fail)
            await reported.wait()
            self.assertEqual(str(failures[1]["exception"]), "callback failed")
            self.assertFalse(timeout.is_active())
            self.assertEqual(self.fiber.state(), FiberState.Active)
        finally:
            loop.set_exception_handler(None)

    async def test_missed_ticks_burst(self) -> None:
        """A delayed loop catches up on elapsed deadlines instead of skipping them."""
        done = asyncio.Event()
        calls: list[int] = []

        def callback() -> None:
            """Create a real scheduling stall, then count the catch-up invocations."""
            calls.append(len(calls))
            if len(calls) == 1:
                time.sleep(0.35)
                done.set()

        handle = self.timer.interval(timedelta(milliseconds=50), callback)
        await done.wait()
        # Count loop turns rather than racing Windows' coarse timer resolution.
        for _ in range(10):
            await asyncio.sleep(0)
        await handle.dispose()
        self.assertGreaterEqual(len(calls), 7)

    async def test_cancelled_sleep_observer_retains_effect(self) -> None:
        """Cancelling observation leaves the registered timer owned by its caller."""
        observer = asyncio.ensure_future(self.timer.sleep(timedelta(seconds=30)))
        await asyncio.sleep(0)
        observer.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await observer
        self.assertEqual(self.fiber.effect_count(), 1)
        await self.fiber.dispose()
        self.assertEqual(self.fiber.effect_count(), 0)
