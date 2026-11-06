"""Validate deterministic cleanup and the real CPython Drop-equivalence boundary."""

import asyncio
import gc
import platform
import unittest
import weakref

from pykardia.core.effect import EffectHandle, run_sync
from pykardia.core.runtime import Runtime
from pykardia.timer.ticks import Disposed, Ticks, TickState


class TickTests(unittest.IsolatedAsyncioTestCase):
    """Exercise the actual tick resource independently of the timer plugin factory."""

    def reader(self) -> tuple[Ticks, TickState, EffectHandle]:
        """Build a real effect whose inverse closes the notification source."""
        state = TickState()
        handle = run_sync(Runtime(), "ctx.interval()", lambda scope: scope.defer_fn(state.close))
        return Ticks(state, handle), state, handle

    async def test_close_and_context_exit_are_deterministic(self) -> None:
        """Explicit close and exception unwinding release the effect immediately."""
        ticks, _, handle = self.reader()
        ticks.close()
        ticks.close()
        self.assertFalse(handle.is_active())
        with self.assertRaisesRegex(Disposed, "^Context has been disposed$"):
            await ticks.tick()
        other, _, handle = self.reader()
        with self.assertRaisesRegex(ValueError, "body"), other:
            raise ValueError("body")
        self.assertFalse(handle.is_active())

    async def test_ticks_are_not_buffered_and_close_wakes_readers(self) -> None:
        """Readers receive only future ticks and fail once their owning effect closes."""
        ticks, state, handle = self.reader()
        state.notify()
        waiting = asyncio.create_task(ticks.tick())
        await asyncio.sleep(0)
        self.assertFalse(waiting.done())
        state.notify()
        await asyncio.wait_for(waiting, 1)
        waiting = asyncio.create_task(ticks.tick())
        await asyncio.sleep(0)
        handle.dispose()
        with self.assertRaises(Disposed):
            await waiting

    @unittest.skipUnless(platform.python_implementation() == "CPython", "CPython refcount probe")
    async def test_acyclic_reader_destruction_disposes_immediately(self) -> None:
        """Confirm the supported interpreter's acyclic object-destruction behavior."""
        ticks, _, handle = self.reader()
        reference = weakref.ref(ticks)
        del ticks
        self.assertIsNone(reference())
        self.assertFalse(handle.is_active())

    async def test_cyclic_reader_needs_explicit_close_or_gc(self) -> None:
        """Record the unavoidable implicit-lifetime gap rather than claiming Rust Drop parity."""

        class CyclicTicks(Ticks):
            """A user-created reference cycle retaining the actual tick reader."""

            def __init__(self, state: TickState, handle: EffectHandle) -> None:
                """Add the reference cycle without changing cleanup behavior."""
                super().__init__(state, handle)
                self.cycle: CyclicTicks = self

        state = TickState()
        handle = run_sync(Runtime(), "ctx.interval()", lambda scope: scope.defer_fn(state.close))
        enabled = gc.isenabled()
        gc.disable()
        try:
            ticks = CyclicTicks(state, handle)
            reference = weakref.ref(ticks)
            del ticks
            self.assertIsNotNone(reference())
            self.assertTrue(handle.is_active())
            gc.collect()
            self.assertIsNone(reference())
            self.assertFalse(handle.is_active())
        finally:
            if enabled:
                gc.enable()
