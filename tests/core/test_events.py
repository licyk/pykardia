"""Event dispatch snapshot, reentry, asynchronous ordering and effect lifetime ports."""

import asyncio
import unittest
from dataclasses import dataclass

import async_timeout

from pykardia import AggregateError, Context, Event, Fiber, JsonValue, PluginDef


class Ping(Event[None]):
    """A unit-output event for lifecycle and listener ordering scenarios."""


class Query(Event[str | None]):
    """A query whose first nonempty result ends sequential dispatch."""


@dataclass
class Payload(Event[int | None]):
    """A distinct event type carrying a concrete payload."""

    value: int


@dataclass
class Session(Event[BaseException | None]):
    """A result-like event filtered by listener context metadata."""

    flag: bool

    def filter(self, listener: Context) -> bool:
        """Select the innermost interception flag when present."""
        values = listener.intercepts("filter")
        if not values:
            return True
        value = values[-1]
        return isinstance(value, dict) and value.get("flag") is self.flag


async def dispatch(root: Context, mode: str) -> None:
    """Exercise the selected real dispatch mode using a unit-output event."""
    if mode == "emit":
        root.emit(Ping())
    elif mode == "parallel":
        await root.parallel(Ping())
    elif mode == "serial":
        await root.serial(Ping())
    else:
        root.bail(Ping())


class EventTests(unittest.IsolatedAsyncioTestCase):
    """Observe actual listener records and runtime tasks through public entrypoints."""

    async def asyncSetUp(self) -> None:
        """Create a root with a finite asynchronous test deadline."""
        self.root = Context()
        self.timeout = async_timeout.timeout(5)
        await self.timeout.__aenter__()

    async def asyncTearDown(self) -> None:
        """Revert every listener and child fiber through normal root disposal."""
        if not self.timeout.expired:
            await self.root.fiber().dispose()
        await self.timeout.__aexit__(None, None, None)

    async def test_registration_prepend_disposal_and_empty_buckets(self) -> None:
        """Preserve prepend ordering and remove only the disposed listener registration."""
        calls: list[str] = []
        first = self.root.on(Ping, lambda event: calls.append("first"))
        second = self.root.on_prepend(Ping, lambda event: calls.append("second"))
        self.root.emit(Ping())
        self.assertEqual(calls, ["second", "first"])
        await second.dispose()
        self.root.emit(Ping())
        self.assertEqual(calls, ["second", "first", "first"])
        self.assertEqual(self.root.listener_count(Ping), 1)
        await first.dispose()
        self.assertEqual(self.root.listener_count(Ping), 0)
        self.assertFalse(self.root._root.read(lambda state: state.listeners))

    async def test_once_reentry_and_concurrent_serial_dispatch(self) -> None:
        """One-shot listeners are consumed before recursive or concurrent snapshots invoke them."""
        calls: list[str] = []
        nested = False

        def recurse(event: Ping) -> None:
            """Dispatch recursively once from an earlier listener."""
            nonlocal nested
            if not nested:
                nested = True
                self.root.emit(event)

        recursive = self.root.on(Ping, recurse)
        once = self.root.once(Ping, lambda event: calls.append("once"))
        self.root.emit(Ping())
        self.root.emit(Ping())
        self.assertEqual(calls, ["once"])
        await asyncio.gather(once.dispose(), recursive.dispose())
        gate = asyncio.Event()

        async def wait(event: Ping) -> None:
            """Hold concurrent serial dispatches before they reach the one-shot entry."""
            await gate.wait()

        self.root.on_async(Ping, wait)
        self.root.once(Ping, lambda event: calls.append("serial once"))
        first = asyncio.create_task(self.root.serial(Ping()))
        second = asyncio.create_task(self.root.serial(Ping()))
        await asyncio.sleep(0)
        gate.set()
        await asyncio.gather(first, second)
        self.assertEqual(calls, ["once", "serial once"])

    async def test_removed_and_added_listeners_across_dispatch_modes(self) -> None:
        """All four modes skip removals and defer additions until the next snapshot."""
        for mode in ("emit", "parallel", "serial", "bail"):
            await self._snapshot_mutation(mode)

    async def _snapshot_mutation(self, mode: str) -> None:
        """Mutate an actual listener list during dispatch without replacing its machinery."""
        root = Context()
        removed: list[str] = []
        remaining: list[str] = []
        added: list[str] = []

        def mutate(event: Ping) -> None:
            """Remove one already captured listener and register another for the next dispatch."""
            handle.dispose()
            root.on(Ping, lambda event: added.append("added"))

        root.on(Ping, mutate)
        handle = root.on(Ping, lambda event: removed.append("removed"))
        root.on(Ping, lambda event: remaining.append("remaining"))
        await dispatch(root, mode)
        self.assertEqual((removed, remaining, added), ([], ["remaining"], []))
        await dispatch(root, mode)
        self.assertEqual((removed, remaining, added), ([], ["remaining", "remaining"], ["added"]))
        await root.fiber().dispose()

    async def test_serial_skips_listener_disposed_with_its_plugin(self) -> None:
        """Awaited disposal by an earlier callback removes a later captured registration."""
        child: Fiber | None = None
        calls: list[str] = []

        async def remove(event: Ping) -> None:
            """Wait for the real child lifecycle to finish before serial dispatch resumes."""
            assert child is not None
            await child.dispose()

        self.root.on_async(Ping, remove)

        async def apply(context: Context, config: JsonValue) -> None:
            """Install a listener owned by the disposable child."""
            context.on(Ping, lambda event: calls.append("child"))

        child = self.root.plugin_def(PluginDef.new(apply))
        await child.settled()
        await self.root.serial(Ping())
        self.assertEqual(calls, [])

    async def test_filters_by_listener_context_for_all_modes(self) -> None:
        """Filter runs against the registration context for every dispatch mode."""
        calls: list[bool] = []
        self.root.intercept("filter", {"flag": True}).on(
            Session, lambda event: calls.append(event.flag)
        )
        self.root.emit(Session(False))
        self.assertEqual(calls, [])
        self.root.emit(Session(True))
        self.root.bail(Session(True))
        await self.root.serial(Session(True))
        await self.root.parallel(Session(True))
        self.assertEqual(calls, [True] * 4)

    async def test_parallel_collects_all_returned_and_raised_errors(self) -> None:
        """Parallel dispatch waits for every callback and keeps failures in listener order."""
        done: list[str] = []
        self.root.on(Session, lambda event: ValueError("returned"))

        def fail(event: Session) -> None:
            """Raise a callback failure corresponding to a reference panic."""
            raise RuntimeError("raised")

        async def asynchronous(event: Session) -> BaseException:
            """Finish asynchronously before returning a result-style error."""
            await asyncio.sleep(0)
            done.append("async")
            return ValueError("async")

        self.root.on(Session, fail)
        self.root.on_async(Session, asynchronous)
        with self.assertRaises(AggregateError) as caught:
            await self.root.parallel(Session(True))
        self.assertEqual(caught.exception.message, "one or more listeners of event Session failed")
        self.assertEqual(
            [str(error) for error in caught.exception.errors], ["returned", "raised", "async"]
        )
        self.assertEqual(done, ["async"])

    async def test_bail_and_serial_first_result_and_result_errors(self) -> None:
        """Bail starts async callbacks but serial can return their first bailing result."""
        seen: list[int] = []
        finished = asyncio.Event()

        def first(event: Query) -> None:
            """Decline the query after recording the first callback."""
            seen.append(1)

        async def asynchronous(event: Query) -> str:
            """Return the asynchronous result for serial dispatch to observe."""
            finished.set()
            return "async"

        def last(event: Query) -> str:
            """Answer synchronous bail after its earlier asynchronous callback starts."""
            seen.append(3)
            return "sync"

        self.root.on(Query, first)
        self.root.on_async(Query, asynchronous)
        self.root.on(Query, last)
        self.assertEqual(self.root.bail(Query()), "sync")
        self.assertEqual(await self.root.serial(Query()), "async")
        await finished.wait()
        self.assertEqual(seen, [1, 3, 1])
        error = ValueError("message")
        self.root.on(Session, lambda event: error)
        self.assertIs(self.root.bail(Session(True)), error)
        self.assertIs(await self.root.serial(Session(True)), error)

    async def test_emit_logs_raised_failure_and_continues(self) -> None:
        """A throwing synchronous listener is logged without preventing later callbacks."""
        calls: list[int] = []

        def fail(event: Ping) -> None:
            """Throw from a real callback invocation."""
            raise RuntimeError("listener failed")

        self.root.on(Ping, fail)
        self.root.on(Ping, lambda event: calls.append(1))
        self.root.emit(Ping())
        self.assertEqual(calls, [1])
        self.assertEqual(self.root.log_buffer()[0].text, "listener failed")

    async def test_payload_type_identity_across_modes(self) -> None:
        """Concrete event classes keep distinct payloads and optional zero still bails."""
        seen: list[int] = []

        def read(event: Payload) -> int:
            """Return a concrete payload without applying Python truthiness."""
            seen.append(event.value)
            return event.value

        handle = self.root.on(Payload, read)
        self.root.emit(Payload(1))
        self.assertEqual(self.root.bail(Payload(0)), 0)
        self.assertEqual(await self.root.serial(Payload(3)), 3)
        await self.root.parallel(Payload(4))
        await handle.dispose()
        self.root.emit(Payload(5))
        self.assertEqual(seen, [1, 0, 3, 4])

    async def test_nested_listener_lifetimes_restore_snapshots(self) -> None:
        """Nested plugin cleanup restores the original root listener and registry counts."""
        calls: list[int] = []
        self.root.on(Ping, lambda event: calls.append(-1))

        def definition(depth: int) -> PluginDef:
            """Build three real nested definitions, each with its own listener effect."""

            async def apply(context: Context, config: JsonValue) -> None:
                """Register the current listener before instantiating the next child."""
                context.on(Ping, lambda event: calls.append(depth))
                if depth:
                    await context.plugin_def(definition(depth - 1)).settled()

            return PluginDef.new(apply)

        plugin = definition(2)
        first = self.root.plugin_def(plugin)
        await first.settled()
        self.assertEqual(self.root.registry().size(), 3)
        self.root.emit(Ping())
        self.assertEqual(calls, [-1, 2, 1, 0])
        await first.dispose()
        self.assertEqual(self.root.listener_count(Ping), 1)
        self.root.emit(Ping())
        self.assertEqual(calls[-1], -1)
        second = self.root.plugin_def(plugin)
        await second.settled()
        self.assertEqual(self.root.listener_count(Ping), 4)

    async def test_internal_insert_status_and_remove_order(self) -> None:
        """Publish insertion/removal before their corresponding queued status notifications."""
        from pykardia import FiberState, PluginInserted, PluginRemoved, StatusChanged

        observed: list[tuple[str, FiberState]] = []
        self.root.on(PluginInserted, lambda event: observed.append(("insert", event.fiber.state())))
        self.root.on(PluginRemoved, lambda event: observed.append(("remove", event.fiber.state())))
        self.root.on(StatusChanged, lambda event: observed.append(("status", event.new)))

        async def apply(context: Context, config: JsonValue) -> None:
            """Run one ordinary episode while its status events are recorded."""

        fiber = self.root.plugin_def(PluginDef.new(apply))
        self.assertEqual(observed, [("insert", FiberState.Loading), ("status", FiberState.Loading)])
        await fiber.settled()
        self.assertEqual(observed[-1], ("status", FiberState.Active))
        pending = fiber.dispose()
        self.assertEqual(
            observed[-2:], [("remove", FiberState.Unloading), ("status", FiberState.Unloading)]
        )
        await pending
        self.assertEqual(observed[-1], ("status", FiberState.Disposed))

    async def test_update_hooks_do_not_accumulate_and_can_be_removed(self) -> None:
        """Only the current episode's hook sees an update, and explicit removal suppresses it."""
        from pykardia import EffectHandle

        received: list[int] = []
        handles: list[EffectHandle] = []

        async def apply(context: Context, config: JsonValue) -> None:
            """Install one declining update hook on every new episode."""

            def accept(value: int) -> bool:
                """Record the offered value and ask the runtime to restart normally."""
                received.append(value)
                return False

            handles.append(context.on_update(int, accept))

        fiber = self.root.plugin_def(PluginDef.new(apply), 0)
        await fiber.settled()
        await fiber.update(1)
        await fiber.update(2)
        self.assertEqual(received, [1, 2])
        await handles[-1].dispose()
        await fiber.update(3)
        self.assertEqual(received, [1, 2])
