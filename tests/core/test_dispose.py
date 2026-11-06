"""Public API ports of crates/kardia-core/tests/dispose.rs."""

import asyncio
import unittest

from pykardia import Context, Disposer, EffectHandle, EffectMeta, EffectScope, Error, FiberState


class DisposeTests(unittest.IsolatedAsyncioTestCase):
    """Drive real contexts with explicit setup gates instead of substituting business logic."""

    async def test_dispose_manually(self) -> None:
        """A manual inverse runs immediately and removes its effect only once."""
        root = Context()
        sequence: list[int] = []
        handle = root.on_dispose(lambda: sequence.append(1))
        self.assertEqual(root.fiber().effects(), [EffectMeta("ctx.on_dispose()", [])])
        disposal = handle.dispose()
        self.assertTrue(disposal.is_ready())
        self.assertEqual(sequence, [1])
        await handle.dispose()
        self.assertEqual(sequence, [1])
        self.assertEqual(root.fiber().effects(), [])

    async def test_yield_dispose(self) -> None:
        """Adopt nested effects, preserving tree introspection and sequential LIFO cleanup."""
        root = Context()
        sequence: list[int] = []

        def nested(scope: EffectScope) -> None:
            """Add a nested child and an inverse to the inner effect."""
            scope.adopt(root.on_dispose(lambda: None))
            scope.defer_fn(lambda: sequence.append(3))

        def body(scope: EffectScope) -> None:
            """Collect the reference's ordered inverse and child sequence."""
            scope.defer_fn(lambda: sequence.append(1))
            scope.adopt(root.on_dispose(lambda: None))
            scope.defer_fn(lambda: sequence.append(2))
            scope.adopt(root.effect_sync("anonymous", nested))

        handle = root.effect_sync("anonymous", body)
        root.on_dispose(lambda: None)
        self.assertEqual(
            root.fiber().effects(),
            [
                EffectMeta(
                    "anonymous",
                    [
                        EffectMeta("ctx.on_dispose()", []),
                        EffectMeta("anonymous", [EffectMeta("ctx.on_dispose()", [])]),
                    ],
                ),
                EffectMeta("ctx.on_dispose()", []),
            ],
        )
        await handle.dispose()
        self.assertEqual(sequence, [3, 2, 1])
        await handle.dispose()
        self.assertEqual(root.fiber().effect_count(), 1)
        await root.fiber().dispose()

    async def test_async_return_1(self) -> None:
        """A completed async body leaves its collected inverse for manual disposal."""
        root = Context()
        gate = asyncio.Event()
        sequence: list[int] = []

        async def body(scope: EffectScope) -> None:
            """Acquire a resource after a controlled await."""
            await gate.wait()
            sequence.append(1)
            scope.defer_fn(lambda: sequence.append(2))

        handle = root.effect("anonymous", body)
        gate.set()
        await handle.ready()
        self.assertEqual(sequence, [1])
        await handle.dispose()
        self.assertEqual(sequence, [1, 2])

    async def test_async_return_2(self) -> None:
        """A discarded disposal completion still waits for setup and releases its resource."""
        root = Context()
        gate, cleaned = asyncio.Event(), asyncio.Event()
        sequence: list[int] = []

        def inverse() -> None:
            """Record resource cleanup and signal actual completion."""
            sequence.append(2)
            cleaned.set()

        async def body(scope: EffectScope) -> None:
            """Finish setup despite a prior disposal request."""
            await gate.wait()
            sequence.append(1)
            scope.defer_fn(inverse)

        root.effect("anonymous", body).dispose()
        self.assertEqual(sequence, [])
        gate.set()
        await asyncio.wait_for(cleaned.wait(), 1)
        self.assertEqual(sequence, [1, 2])

    async def test_async_yield_boundaries_and_ready(self) -> None:
        """Port normal, pre-first and post-first interruption of three_steps and ready."""
        for completed in (0, 1, 3):
            with self.subTest(completed=completed):
                root = Context()
                sequence: list[int] = []
                gates = [asyncio.Event() for _ in range(3)]
                reached = [asyncio.Event() for _ in range(3)]
                handle = self.three_steps(root, sequence, gates, reached)
                for index in range(completed):
                    gates[index].set()
                    await asyncio.wait_for(reached[index].wait(), 1)
                if completed == 3:
                    await handle.ready()
                disposal = handle.dispose()
                for gate in gates:
                    gate.set()
                await disposal
                expected = {0: [1, 2], 1: [1, 3, 4, 2], 3: [1, 3, 5, 6, 4, 2]}
                self.assertEqual(sequence, expected[completed])
                self.assertEqual(root.log_buffer(), [])
                self.assertEqual(root.fiber().effect_count(), 0)

    def three_steps(
        self,
        root: Context,
        sequence: list[int],
        gates: list[asyncio.Event],
        reached: list[asyncio.Event],
    ) -> EffectHandle:
        """Run the reference's three resource-acquisition steps through real EffectScope."""

        async def body(scope: EffectScope) -> None:
            """Collect each inverse before the next cooperative boundary."""
            for index, value in enumerate((1, 3, 5)):
                await gates[index].wait()
                sequence.append(value)
                scope.yield_(Disposer.new(lambda value=value: sequence.append(value + 1)))
                reached[index].set()

        return root.effect("anonymous", body)

    async def test_async_yield_aborted_finally(self) -> None:
        """Python finally executes before the collected inverses after interruption."""
        root = Context()
        gate, entered = asyncio.Event(), asyncio.Event()
        sequence: list[int] = []

        async def body(scope: EffectScope) -> None:
            """Unwind ordinary Python state at the explicit iteration boundary."""
            try:
                scope.yield_(Disposer.new(lambda: sequence.append(1)))
                entered.set()
                await gate.wait()
                scope.yield_(Disposer.new(lambda: sequence.append(2)))
                scope.yield_(Disposer.new(lambda: sequence.append(3)))
            finally:
                sequence.append(0)

        handle = root.effect("anonymous", body)
        await entered.wait()
        disposal = handle.dispose()
        gate.set()
        await disposal
        self.assertEqual(sequence, [0, 2, 1])

    async def test_async_yield_4_await_ready(self) -> None:
        """An already-waiting ready observer cannot finish before the effect's final step."""
        root = Context()
        sequence: list[int] = []
        gates = [asyncio.Event() for _ in range(3)]
        reached = [asyncio.Event() for _ in range(3)]
        handle = self.three_steps(root, sequence, gates, reached)
        waiting = asyncio.create_task(handle.ready().wait())
        await asyncio.sleep(0)
        self.assertFalse(waiting.done())
        for gate in gates:
            gate.set()
        await asyncio.wait_for(waiting, 1)
        self.assertEqual(sequence, [1, 3, 5])
        await handle.dispose()
        self.assertEqual(sequence, [1, 3, 5, 6, 4, 2])

    async def test_sync_failures_clean_collected_resources(self) -> None:
        """Port return_with_error, yield_with_error and panic_in_body_is_an_error."""
        for collect in (False, True):
            for error in (Error.msg("test"), RuntimeError("boom")):
                with self.subTest(collect=collect, error=type(error).__name__):
                    root = Context()
                    sequence: list[int] = []

                    def body(
                        scope: EffectScope,
                        collect: bool = collect,
                        sequence: list[int] = sequence,
                        error: BaseException = error,
                    ) -> None:
                        """Optionally collect a resource before failing synchronously."""
                        if collect:
                            scope.defer_fn(lambda: sequence.append(1))
                        raise error

                    with self.assertRaises(type(error)):
                        root.effect_sync("anonymous", body)
                    self.assertEqual(sequence, [1] if collect else [])
                    self.assertEqual(root.fiber().effect_count(), 0)

    async def test_async_failures_log_and_clean_once(self) -> None:
        """Port async_return_with_error and async_yield_with_error."""
        for collect in (False, True):
            with self.subTest(collect=collect):
                root = Context()
                sequence: list[int] = []

                async def body(
                    scope: EffectScope, collect: bool = collect, sequence: list[int] = sequence
                ) -> None:
                    """Optionally collect a resource before reporting a setup failure."""
                    if collect:
                        scope.yield_(Disposer.new(lambda: sequence.append(1)))
                    raise Error.msg("test")

                handle = root.effect("anonymous", body)
                with self.assertRaisesRegex(Error, "test"):
                    await handle.ready()
                self.assertEqual([message.text for message in root.log_buffer()], ["test"])
                self.assertEqual(sequence, [1] if collect else [])
                await handle.dispose()
                self.assertEqual(sequence, [1] if collect else [])

    async def test_root_disposal_restarts_and_drains_effects_concurrently(self) -> None:
        """Fiber cleanup starts newest first but does not serialize separate effects."""
        root = Context()
        gate = asyncio.Event()
        started, other_finished = asyncio.Event(), asyncio.Event()
        sequence: list[int] = []

        async def latest() -> None:
            """Wait for another effect's inverse to prove cleanup concurrency."""
            sequence.append(2)
            started.set()
            await gate.wait()

        async def older() -> None:
            """Finish even while the newest effect is blocked."""
            await started.wait()
            sequence.append(1)
            other_finished.set()

        root.defer("old", Disposer.future(older))
        root.defer("new", Disposer.future(latest))
        disposal = root.fiber().dispose()
        self.assertEqual(root.fiber().state(), FiberState.Unloading)
        self.assertFalse(disposal.is_ready())
        await asyncio.wait_for(other_finished.wait(), 1)
        gate.set()
        await disposal
        self.assertEqual(sequence, [2, 1])
        self.assertEqual(root.fiber().state(), FiberState.Active)
        self.assertFalse(root.fiber().is_removed())
        self.assertEqual(root.fiber().effect_count(), 0)
        root.on_dispose(lambda: sequence.append(3))
        await root.fiber().dispose()
        self.assertEqual(sequence, [2, 1, 3])

    async def test_disposers_run_in_reverse_and_survive_failures(self) -> None:
        """Port the public mixed sync/async inverse sequence including first-error precedence."""
        root = Context()
        sequence: list[int] = []

        async def middle() -> None:
            """Yield before recording the asynchronous inverse."""
            await asyncio.sleep(0)
            sequence.append(2)

        def fail() -> None:
            """Fail without preventing the remaining inverses from running."""
            raise Error.msg("failed")

        def body(scope: EffectScope) -> None:
            """Collect exactly the reference's mixture of four inverse actions."""
            scope.defer_fn(lambda: sequence.append(1))
            scope.defer(Disposer.future(middle))
            scope.defer(Disposer.fallible(fail))
            scope.defer_fn(lambda: sequence.append(3))

        handle = root.effect_sync("anonymous", body)
        disposal = handle.dispose()
        self.assertFalse(disposal.is_ready())
        self.assertEqual(sequence, [3])
        self.assertEqual(root.fiber().effect_count(), 0)
        with self.assertRaisesRegex(Error, "failed"):
            await disposal
        self.assertEqual(sequence, [3, 2, 1])

    async def test_root_cleanup_failure_is_logged_without_failing_restart(self) -> None:
        """A root's failed inverse is reported while its new episode still becomes Active."""
        root = Context()

        def fail() -> None:
            """Report a cleanup error through the real root transition."""
            raise ValueError("cleanup failed")

        root.on_dispose(fail)
        await root.fiber().dispose()
        self.assertEqual(root.fiber().state(), FiberState.Active)
        self.assertEqual([message.text for message in root.log_buffer()], ["cleanup failed"])

    async def test_cleanup_reenters_root_without_a_state_lock(self) -> None:
        """An inverse can inspect and register resources on its own live root."""
        root = Context()
        counts: list[int] = []

        def inverse() -> None:
            """Re-enter the actual public API during cleanup."""
            counts.append(root.fiber().effect_count())
            root.on_dispose(lambda: counts.append(99))

        root.on_dispose(inverse).dispose()
        self.assertEqual(counts, [0])
        self.assertEqual(root.fiber().effect_count(), 1)
        await root.fiber().dispose()
        self.assertEqual(counts, [0, 99])
