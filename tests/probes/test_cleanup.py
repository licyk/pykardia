"""Exercise the production cleanup engine against effect.rs ordering rules."""

import asyncio
import unittest

from pykardia.core.cleanup import Disposer, run_sequence
from pykardia.core.runtime import Runtime


class CleanupTests(unittest.IsolatedAsyncioTestCase):
    """Verify synchronous execution, sequential async cleanup and failure handling."""

    async def test_synchronous_prefix_and_first_failure(self) -> None:
        """Port disposers_run_in_reverse_and_survive_failures with an explicit gate."""
        runtime = Runtime()
        sequence: list[int] = []
        gate = asyncio.Event()
        failure = ValueError("failed")

        async def middle() -> None:
            """Wait until the test lets the asynchronous inverse finish."""
            await gate.wait()
            sequence.append(2)

        def fail() -> None:
            """Report the first cleanup failure."""
            raise failure

        disposal = run_sequence(
            runtime,
            [
                Disposer.new(lambda: sequence.append(1)),
                Disposer.future(middle),
                Disposer.fallible(fail),
                Disposer.new(lambda: sequence.append(3)),
            ],
        )
        self.assertEqual(sequence, [3])
        self.assertFalse(disposal.is_ready())
        gate.set()
        with self.assertRaises(ValueError) as caught:
            await disposal
        self.assertIs(caught.exception, failure)
        self.assertEqual(sequence, [3, 2, 1])
        self.assertFalse(disposal.is_ready())

    async def test_ready_cleanup_consumes_callback_and_error_once(self) -> None:
        """Synchronous cleanup finishes before return and cannot repeat its inverse."""
        calls: list[int] = []

        def fail() -> None:
            """Record the cleanup and fail."""
            calls.append(1)
            raise ValueError("once")

        disposer = Disposer.new(fail)
        disposal = run_sequence(Runtime(), [disposer])
        self.assertEqual(calls, [1])
        self.assertTrue(disposal.is_ready())
        with self.assertRaisesRegex(ValueError, "once"):
            await disposal
        await disposal
        await run_sequence(Runtime(), [disposer])
        self.assertEqual(calls, [1])

    async def test_async_factory_prefix_and_dropped_observer(self) -> None:
        """The factory runs now and its returned coroutine survives a dropped result."""
        calls: list[str] = []
        done = asyncio.Event()

        async def finish() -> None:
            """Complete on the runtime loop."""
            calls.append("finish")
            done.set()

        def start() -> asyncio.Future[None]:
            """Record the synchronous prefix and return the scheduled continuation."""
            calls.append("start")
            return asyncio.ensure_future(finish())

        run_sequence(Runtime(), [Disposer.future(start)])
        self.assertEqual(calls, ["start"])
        await asyncio.wait_for(done.wait(), 1)
        self.assertEqual(calls, ["start", "finish"])

    async def test_async_failures_continue_strictly_in_sequence(self) -> None:
        """A later inverse cannot start before the previous asynchronous one finishes."""
        sequence: list[int] = []
        gate = asyncio.Event()
        started = asyncio.Event()

        async def first() -> None:
            """Start, wait, and raise the winning failure."""
            sequence.append(3)
            started.set()
            await gate.wait()
            raise ValueError("first")

        async def second() -> None:
            """Run only after first has completed, despite its failure."""
            sequence.append(2)
            raise ValueError("second")

        disposal = run_sequence(
            Runtime(),
            [
                Disposer.new(lambda: sequence.append(1)),
                Disposer.future(second),
                Disposer.future(first),
            ],
        )
        await started.wait()
        self.assertEqual(sequence, [3])
        gate.set()
        with self.assertRaisesRegex(ValueError, "first"):
            await disposal
        self.assertEqual(sequence, [3, 2, 1])
