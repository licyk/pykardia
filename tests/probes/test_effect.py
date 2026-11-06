"""Probe cooperative setup using the actual reusable effect collector."""

import asyncio
import unittest

from pykardia.core.cleanup import Disposer
from pykardia.core.effect import EffectCell, EffectHandle, EffectMeta, EffectScope, run_sync
from pykardia.core.runtime import Runtime


class EffectTests(unittest.IsolatedAsyncioTestCase):
    """Port effect cleanup races before attaching the fiber state machine."""

    async def test_dispose_waits_for_late_resource_and_checkpoint(self) -> None:
        """Superseded setup continues past an await and releases what it just acquired."""
        cell = EffectCell(Runtime(), "setup")
        handle = EffectHandle(cell)
        gate = asyncio.Event()
        entered = asyncio.Event()
        sequence: list[int] = []
        errors: list[BaseException] = []

        async def body(scope: EffectScope) -> None:
            """Acquire another resource after disposal starts, then cooperatively exit."""
            try:
                scope.defer_fn(lambda: sequence.append(1))
                entered.set()
                await gate.wait()
                sequence.append(2)
                scope.yield_(Disposer.new(lambda: sequence.append(3)))
                sequence.append(99)
            finally:
                sequence.append(0)

        cell.start(body, errors.append)
        await entered.wait()
        disposal = handle.dispose()
        self.assertFalse(handle.is_active())
        self.assertFalse(disposal.is_ready())
        self.assertEqual(sequence, [])
        gate.set()
        await disposal
        await handle.ready()
        self.assertEqual(sequence, [2, 0, 3, 1])
        self.assertEqual(errors, [])

    async def test_discarded_disposal_waits_for_setup_without_checkpoint(self) -> None:
        """Setup with no boundary runs to completion before its late inverse is drained."""
        cell = EffectCell(Runtime(), "late")
        handle = EffectHandle(cell)
        gate = asyncio.Event()
        cleaned = asyncio.Event()
        errors: list[BaseException] = []

        async def body(scope: EffectScope) -> None:
            """Register the inverse after the owner has already requested cleanup."""
            await gate.wait()
            scope.defer_fn(cleaned.set)

        cell.start(body, errors.append)
        handle.dispose()
        gate.set()
        await asyncio.wait_for(cleaned.wait(), 1)
        self.assertEqual(errors, [])

    async def test_late_inverse_runs_immediately_after_close(self) -> None:
        """A saved scope cannot leak resources added after final cleanup."""
        cell = EffectCell(Runtime(), "late")
        scope = EffectScope(cell)
        await cell.dispose()
        sequence: list[int] = []
        scope.defer_fn(lambda: sequence.append(1))
        self.assertEqual(sequence, [1])

    async def test_failure_cleanup_logs_then_ready_reports_setup_error(self) -> None:
        """Cleanup errors precede setup errors in logs without replacing the setup result."""
        runtime = Runtime()
        cell = EffectCell(runtime, "failed")
        errors: list[BaseException] = []
        setup_error = ValueError("setup")
        cleanup_error = ValueError("cleanup")
        scopes: list[EffectScope] = []
        sequence: list[int] = []

        def cleanup() -> None:
            """Report a cleanup failure."""
            raise cleanup_error

        async def body(scope: EffectScope) -> None:
            """Collect a failing inverse and fail setup."""
            scopes.append(scope)
            scope.defer_fn(cleanup)
            raise setup_error

        cell.start(body, errors.append)
        with self.assertRaises(ValueError) as caught:
            await cell.ready()
        self.assertIs(caught.exception, setup_error)
        self.assertEqual(errors, [cleanup_error, setup_error])
        scopes[0].defer_fn(lambda: sequence.append(1))
        self.assertEqual(sequence, [])
        await cell.dispose()
        self.assertEqual(sequence, [1])

    async def test_adoption_preserves_synchronous_lifo_and_metadata(self) -> None:
        """Nested effects leave their previous accumulator and join the parent's LIFO."""
        runtime = Runtime()
        detached: list[str] = []
        sequence: list[int] = []
        child = run_sync(runtime, "child", lambda scope: scope.defer_fn(lambda: sequence.append(2)))
        child._cell.set_detach(lambda: detached.append("child"))

        def body(scope: EffectScope) -> None:
            """Surround the adopted child with two ordinary inverses."""
            scope.defer_fn(lambda: sequence.append(1))
            scope.adopt(child)
            scope.defer_fn(lambda: sequence.append(3))

        parent = run_sync(runtime, "parent", body)
        self.assertEqual(detached, ["child"])
        self.assertEqual(parent.meta(), EffectMeta("parent", [EffectMeta("child", [])]))
        disposal = parent.dispose()
        self.assertTrue(disposal.is_ready())
        self.assertEqual(sequence, [3, 2, 1])
        await disposal
        await child.dispose()
        self.assertEqual(sequence, [3, 2, 1])

    async def test_sync_failure_cleans_before_propagation(self) -> None:
        """A failing synchronous body never leaves its collected resources active."""
        sequence: list[int] = []

        def body(scope: EffectScope) -> None:
            """Collect an inverse then fail."""
            scope.defer_fn(lambda: sequence.append(1))
            raise ValueError("body")

        with self.assertRaisesRegex(ValueError, "body"):
            run_sync(Runtime(), "failed", body)
        self.assertEqual(sequence, [1])
