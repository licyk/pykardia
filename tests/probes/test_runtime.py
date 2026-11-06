"""Exercise the actual root driver before attaching the fiber state machine."""

import asyncio
import gc
import unittest

from pykardia.core.runtime import Action, Completion, Runtime


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    """Check task ownership, atomic prefixes, cancellation and thread entry."""

    async def test_discarded_completion_keeps_running(self) -> None:
        """Drop the completion while the root retains the operation."""
        runtime = Runtime()
        done = asyncio.Event()

        async def work() -> None:
            """Finish after yielding to the event loop."""
            await asyncio.sleep(0)
            done.set()

        runtime.spawn(work())
        await asyncio.wait_for(done.wait(), 1)

    async def test_waiter_cancellation_does_not_cancel_work(self) -> None:
        """Cancel one waiter and let another observe the same actual task result."""
        runtime = Runtime()
        gate = asyncio.Event()

        async def work() -> int:
            """Return after the caller opens the gate."""
            await gate.wait()
            return 42

        completion = runtime.spawn(work())
        waiter = asyncio.create_task(completion.wait())
        await asyncio.sleep(0)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        gate.set()
        self.assertEqual(await completion, 42)
        self.assertEqual(await completion, 42)
        self.assertFalse(completion.is_ready())

    async def test_state_prefix_and_reentrant_action(self) -> None:
        """Commit state before executing an action that re-enters the root."""
        runtime = Runtime()
        values: list[int] = []

        def action() -> None:
            """Read through the lock after the enclosing state segment releases it."""
            values.append(runtime.read(lambda: values[0]) + 1)

        def change() -> tuple[int, list[Action]]:
            """Mutate state and return its post-lock callback."""
            values.append(1)
            return 7, [action]

        self.assertEqual(runtime.run(change), 7)
        self.assertEqual(values, [1, 2])

    async def test_external_thread_schedules_on_owner_loop(self) -> None:
        """Marshal spawned work from a worker thread to the bound loop."""
        loop = asyncio.get_running_loop()
        runtime = Runtime(loop)

        async def work() -> bool:
            """Report whether execution occurs on the original event loop."""
            return asyncio.get_running_loop() is loop

        completion = await asyncio.to_thread(lambda: runtime.spawn(work()))
        self.assertTrue(await completion)

    async def test_state_wait_is_not_lost_or_canceled_by_other_waiters(self) -> None:
        """Wake remaining waiters after another observation is canceled."""
        runtime = Runtime()
        values: list[int] = []
        first = asyncio.create_task(runtime.wait_until(lambda: bool(values)))
        second = asyncio.create_task(runtime.wait_until(lambda: bool(values)))
        await asyncio.sleep(0)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first

        def change() -> tuple[None, list[Action]]:
            """Make both observers' shared predicate true."""
            values.append(1)
            return None, []

        await asyncio.to_thread(lambda: runtime.run(change))
        await asyncio.wait_for(second, 1)

    async def test_ready_results_and_error_identity(self) -> None:
        """Keep synchronous completion classification and the original error object."""
        result = Completion.ready(42)
        self.assertTrue(result.is_ready())
        self.assertEqual(await result, 42)
        error = ValueError("failed")
        failed: Completion[None] = Completion.failed(error)
        with self.assertRaises(ValueError) as caught:
            await failed
        self.assertIs(caught.exception, error)

    async def test_canceled_observer_does_not_leave_unhandled_failure(self) -> None:
        """A failing task remains observable without orphaned asyncio-future warnings."""
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        unhandled: list[dict[str, object]] = []

        def report(_: asyncio.AbstractEventLoop, context: dict[str, object]) -> None:
            """Capture actual event-loop reports during the cancellation scenario."""
            unhandled.append(context)

        loop.set_exception_handler(report)
        gate = asyncio.Event()

        async def fail() -> None:
            """Fail only after the original observer has gone away."""
            await gate.wait()
            raise ValueError("after cancellation")

        try:
            completion = Runtime().spawn(fail())
            waiter = asyncio.create_task(completion.wait())
            await asyncio.sleep(0)
            waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiter
            gate.set()
            with self.assertRaisesRegex(ValueError, "after cancellation"):
                await completion
            del completion, waiter
            await asyncio.sleep(0)
            gc.collect()
            self.assertEqual(unhandled, [])
        finally:
            loop.set_exception_handler(previous_handler)
