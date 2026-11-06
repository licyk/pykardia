"""Real asynchronous provider/consumer teardown and binding guard regressions."""

import asyncio
import unittest
from dataclasses import dataclass

import async_timeout

from pykardia import Context, Disposer, EffectScope, Fiber, FiberState, JsonValue, PluginDef
from tests.core.test_fiber import noop, until


@dataclass
class Resource:
    """A service resource whose closure must follow all dependent cleanup."""

    closed: bool = False


class Fixture:
    """Providers and gated readers sharing an observable resource and timeline."""

    def __init__(self, root: Context) -> None:
        """Allocate gates controlling cleanup of actual runtime instances."""
        self.root = root
        self.timeline: list[str] = []
        self.gate = asyncio.Event()
        self.draining = asyncio.Event()
        self.resource = Resource()

    async def provide(self, context: Context, config: JsonValue) -> None:
        """Provide a service and retain its resource until the final inverse runs."""
        self.resource.closed = False
        context.provide_as("foo", self.resource)

        def close() -> None:
            """Close only after committed consumer guards have been released."""
            self.resource.closed = True
            self.timeline.append("provider closed")

        context.on_dispose(close)

    async def read(self, context: Context, config: JsonValue) -> None:
        """Register a gated inverse that resolves its service again during cleanup."""

        async def cleanup() -> None:
            """Read the committed service after external code permits teardown to proceed."""
            self.draining.set()
            await self.gate.wait()
            value = context.get_as("foo", Resource).arc()
            self.timeline.append("consumer read: closed" if value.closed else "consumer read: open")

        context.defer("read", Disposer.future(cleanup))

    async def provider(self, context: Context | None = None) -> Fiber:
        """Start a real provider under the selected owner."""
        fiber = (context or self.root).plugin_def(PluginDef.new(self.provide).name("provider"))
        await fiber.settled()
        return fiber

    async def consumer(self, context: Context | None = None, *, inject: bool = True) -> Fiber:
        """Start a gated reader, optionally inheriting its parent's admitted dependency."""
        definition = PluginDef.new(self.read).inject(["foo"] if inject else [])
        fiber = (context or self.root).plugin_def(definition)
        await fiber.settled()
        return fiber


class ServiceDisposalTests(unittest.IsolatedAsyncioTestCase):
    """Verify both provider and individual binding guards against actual cleanup tasks."""

    async def asyncSetUp(self) -> None:
        """Create an independent root and finite gate-driven scenario deadline."""
        self.root = Context()
        self.timeout = async_timeout.timeout(5)
        await self.timeout.__aenter__()

    async def asyncTearDown(self) -> None:
        """Release all remaining resources after gates are open."""
        if not self.timeout.expired:
            await self.root.fiber().dispose()
        await self.timeout.__aexit__(None, None, None)

    async def test_resources_survive_all_provider_shutdown_actions(self) -> None:
        """Port retention through dispose, restart, update, replacement and root teardown."""
        for action in (
            "dispose",
            "restart",
            "update",
            "replace",
            "root",
            "consumer first",
            "provider first",
        ):
            with self.subTest(action=action):
                await self._retention(action)

    async def _retention(self, action: str) -> None:
        """Keep the same resource readable until its gated consumer finishes."""
        fx = Fixture(self.root.isolate("foo"))
        provider = await fx.provider()
        consumer = await fx.consumer()
        if action == "restart":
            pending = provider.restart()
        elif action == "update":
            pending = provider.update(1)
        elif action == "replace":
            definition = provider.def_()
            assert definition is not None
            self.root.registry().replace(definition, PluginDef.new(noop))
            pending = provider.settled()
        elif action == "consumer first":
            pending = asyncio.gather(consumer.dispose(), provider.dispose())
        elif action == "provider first":
            pending = asyncio.gather(provider.dispose(), consumer.dispose())
        elif action == "root":
            pending = self.root.fiber().dispose()
        else:
            pending = provider.dispose()
        await fx.draining.wait()
        self.assertFalse(fx.resource.closed)
        fx.gate.set()
        await pending
        self.assertEqual(fx.timeline[:2], ["consumer read: open", "provider closed"])
        await consumer.dispose()
        await provider.dispose()

    async def test_withdraw_waits_for_retired_consumer(self) -> None:
        """A consumer removed from registry still holds its committed binding guard."""
        fx = Fixture(self.root)
        provider = self.root.plugin_def(PluginDef.new(noop))
        await provider.settled()
        handle = provider.context().provide_as("foo", fx.resource)
        consumer = await fx.consumer()
        definition = consumer.def_()
        assert definition is not None
        disposing = consumer.dispose()
        self.assertFalse(self.root.registry().has(definition))
        withdrawal = asyncio.ensure_future(handle.dispose())
        await fx.draining.wait()
        self.assertFalse(withdrawal.done())
        fx.gate.set()
        await asyncio.gather(disposing, withdrawal)
        self.assertEqual(fx.timeline, ["consumer read: open"])

    async def test_binding_guard_does_not_wait_for_unrelated_binding(self) -> None:
        """Withdrawing foo does not wait for another service's consumer from the same provider."""
        gate, draining = asyncio.Event(), asyncio.Event()
        provider = self.root.plugin_def(PluginDef.new(noop))
        await provider.settled()
        handle = provider.context().provide_as("foo", 1)
        provider.context().provide_as("bar", 2)
        first = self.root.plugin_def(PluginDef.new(noop).inject(["foo"]))

        async def consume(context: Context, config: JsonValue) -> None:
            """Hold only the bar consumer's cleanup."""

            async def cleanup() -> None:
                """Signal the unrelated cleanup has started and wait for release."""
                draining.set()
                await gate.wait()

            context.defer("bar", Disposer.future(cleanup))

        second = self.root.plugin_def(PluginDef.new(consume).inject(["bar"]))
        await asyncio.gather(first.settled(), second.settled())
        disposing = second.dispose()
        await draining.wait()
        await handle.dispose()
        self.assertEqual(second.state(), FiberState.Unloading)
        gate.set()
        await disposing

    async def test_inherited_dependency_and_explicit_child_dependency(self) -> None:
        """Children retain both ancestor injections and directly injected parent services."""
        for explicit in (False, True):
            await self._child_dependency(explicit)

    async def _child_dependency(self, explicit: bool) -> None:
        """Dispose a provider only after its descendant reader finishes."""
        fx = Fixture(self.root.isolate("foo"))
        provider = await fx.provider()
        if explicit:
            parent = provider
        else:
            parent = fx.root.plugin_def(PluginDef.new(noop).inject(["foo"]))
            await parent.settled()
        child = await fx.consumer(parent.context(), inject=explicit)
        disposal = provider.dispose() if explicit else self.root.fiber().dispose()
        await fx.draining.wait()
        self.assertFalse(fx.resource.closed)
        fx.gate.set()
        await disposal
        self.assertEqual(fx.timeline, ["consumer read: open", "provider closed"])
        self.assertEqual(child.state(), FiberState.Disposed)

    async def test_adopted_child_reads_inherited_service_during_drain(self) -> None:
        """A composed child can read an own provision withdrawn earlier in the same drain."""
        children: list[Fiber] = []
        reads: list[int] = []

        async def child(context: Context, config: JsonValue) -> None:
            """Register a cleanup read through the parent's own provision."""
            context.on_dispose(lambda: reads.append(context.get_as("foo", int).arc()))

        async def owner(context: Context, config: JsonValue) -> None:
            """Adopt a real child fiber into a composed collector."""
            context.provide_as("foo", 8)

            def compose(scope: EffectScope) -> None:
                """Move the child instantiation out of the direct accumulator."""
                fiber = context.plugin_def(PluginDef.new(child))
                children.append(fiber)
                scope.adopt(fiber)

            context.effect_sync("composed", compose)

        parent = self.root.plugin_def(PluginDef.new(owner))
        await parent.settled()
        await children[0].settled()
        self.assertEqual(parent.effect_count(), 2)
        await parent.dispose()
        self.assertEqual(reads, [8])
        self.assertEqual(self.root.log_buffer(), [])

    async def test_retirement_frees_slot_before_old_consumers_finish(self) -> None:
        """A successor can provide immediately while old consumers still read the old value."""
        fx = Fixture(self.root)
        provider = await fx.provider()
        consumer = await fx.consumer()
        disposing = provider.dispose()
        await fx.draining.wait()
        self.root.provide_as("foo", Resource())
        self.assertIs(consumer.context().get_as("foo", Resource).arc(), fx.resource)
        fx.gate.set()
        await disposing
        await consumer.settled()
        self.assertIsNot(consumer.context().get_as("foo", Resource).arc(), fx.resource)
        self.assertEqual(fx.timeline[:2], ["consumer read: open", "provider closed"])

    async def test_old_withdrawal_finishes_before_new_consumer_setup(self) -> None:
        """The old binding guard tracks its episode, not the consumer's future episode."""
        cleanup_gate, new_setup, started = asyncio.Event(), asyncio.Event(), asyncio.Event()
        versions: list[int] = []
        first = self.root.provide_as("foo", 1)

        async def consume(context: Context) -> None:
            """Read the current version during apply and the committed one during cleanup."""
            value = context.get_as("foo", int).arc()
            versions.append(value)
            if value == 2:
                started.set()
                await new_setup.wait()

            async def cleanup() -> None:
                """Keep the old binding guard until cleanup explicitly completes."""
                await cleanup_gate.wait()
                versions.append(context.get_as("foo", int).arc())

            context.defer("cleanup", Disposer.future(cleanup))

        consumer = self.root.inject(["foo"], consume)
        await consumer.settled()
        withdrawal = first.dispose()
        self.root.provide_as("foo", 2)
        cleanup_gate.set()
        await withdrawal
        await started.wait()
        self.assertEqual(consumer.state(), FiberState.Loading)
        self.assertEqual(versions, [1, 1, 2])
        new_setup.set()
        await consumer.settled()

    async def test_provider_waits_for_current_and_withdrawn_bindings(self) -> None:
        """Provider teardown waits for both classes of binding regardless of finish order."""
        for withdrawn_first in (False, True):
            await self._current_and_withdrawn(withdrawn_first)

    async def _current_and_withdrawn(self, withdrawn_first: bool) -> None:
        """Release two committed consumers in a controlled order."""
        fx = Fixture(self.root.isolate("foo").isolate("bar"))
        provider = await fx.provider()
        old = provider.context().provide_as("bar", 1)
        await fx.consumer()
        bar_gate, bar_draining = asyncio.Event(), asyncio.Event()

        async def consume(context: Context) -> None:
            """Keep the withdrawn bar binding retained until its separate gate opens."""

            async def cleanup() -> None:
                """Complete one independent dependent episode."""
                bar_draining.set()
                await bar_gate.wait()
                fx.timeline.append("bar finished")

            context.defer("bar", Disposer.future(cleanup))

        bar = fx.root.inject(["bar"], consume)
        await bar.settled()
        withdrawal = old.dispose()
        provider.context().provide_as("bar", 2)
        disposing = provider.dispose()
        await asyncio.gather(fx.draining.wait(), bar_draining.wait())
        self.assertFalse(fx.resource.closed)
        first, second = (bar_gate, fx.gate) if withdrawn_first else (fx.gate, bar_gate)
        first.set()
        await until(lambda: len(fx.timeline) == 1)
        self.assertFalse(fx.resource.closed)
        second.set()
        await asyncio.gather(withdrawal, disposing)
        expected = (
            ["bar finished", "consumer read: open"]
            if withdrawn_first
            else ["consumer read: open", "bar finished"]
        )
        self.assertEqual(fx.timeline, [*expected, "provider closed"])

    async def test_setup_failure_and_cleanup_failure_release_guards(self) -> None:
        """In-flight setup keeps dependencies alive even when setup or its inverse fails."""
        for setup_failure in (False, True):
            await self._failing_consumer(setup_failure)

    async def _failing_consumer(self, setup_failure: bool) -> None:
        """Read a provider during rollback and ensure its guard is eventually released."""
        fx = Fixture(self.root.isolate("foo"))
        started, release = asyncio.Event(), asyncio.Event()
        provider = await fx.provider()

        async def consume(context: Context) -> None:
            """Collect an inverse before the asynchronous setup can be superseded."""

            def cleanup() -> None:
                """Read the still-open dependency and optionally fail cleanup."""
                self.assertFalse(context.get_as("foo", Resource).arc().closed)
                fx.timeline.append("consumer cleanup")
                if not setup_failure:
                    raise ValueError("cleanup failed")

            context.on_dispose(cleanup)
            started.set()
            await release.wait()
            self.assertFalse(context.get_as("foo", Resource).arc().closed)
            if setup_failure:
                raise ValueError("setup failed")

        consumer = fx.root.inject(["foo"], consume)
        await started.wait()
        disposing = provider.dispose()
        self.assertFalse(fx.resource.closed)
        release.set()
        await disposing
        self.assertEqual(fx.timeline, ["consumer cleanup", "provider closed"])
        self.assertEqual(consumer.state(), FiberState.Pending)

    async def test_unrelated_providers_cleanup_concurrently(self) -> None:
        """One provider's guard does not serialize unrelated providers or sibling inverses."""
        fx = Fixture(self.root)
        first = await fx.provider()
        await fx.consumer()
        unrelated = self.root.plugin_def(PluginDef.new(noop))
        await unrelated.settled()
        finished: list[str] = []
        unrelated.context().on_dispose(lambda: finished.append("other provider"))
        sibling = self.root.on_dispose(lambda: finished.append("sibling"))
        blocked = first.dispose()
        await fx.draining.wait()
        await unrelated.dispose()
        await sibling.dispose()
        self.assertEqual(finished, ["other provider", "sibling"])
        self.assertFalse(fx.resource.closed)
        fx.gate.set()
        await blocked

    async def test_transitive_dependencies_and_final_consumer_event(self) -> None:
        """Retain upstream resources and listeners through the final downstream cleanup event."""
        from pykardia import Event

        class Final(Event[None]):
            """A final event emitted from a consumer's inverse."""

        fx = Fixture(self.root)
        provider = await fx.provider()
        provider.context().on(Final, lambda event: fx.timeline.append("event received"))

        async def middle(context: Context, config: JsonValue) -> None:
            """Provide bar while holding a committed dependency on foo."""
            context.provide_as("bar", None)

            def cleanup() -> None:
                """Read upstream after downstream cleanup, before the provider closes."""
                self.assertFalse(context.get_as("foo", Resource).arc().closed)
                fx.timeline.append("middle read")

            context.on_dispose(cleanup)

        async def last(context: Context) -> None:
            """Gate final cleanup and emit while upstream listeners must still be registered."""

            async def cleanup() -> None:
                """Notify an upstream provider before releasing the transitive dependency."""
                fx.draining.set()
                await fx.gate.wait()
                context.emit(Final())
                fx.timeline.append("last finished")

            context.defer("last", Disposer.future(cleanup))

        intermediate = self.root.plugin_def(PluginDef.new(middle).inject(["foo"]))
        await intermediate.settled()
        final = self.root.inject(["bar"], last)
        await final.settled()
        pending = self.root.fiber().dispose()
        await fx.draining.wait()
        self.assertFalse(fx.resource.closed)
        fx.gate.set()
        await pending
        self.assertEqual(
            fx.timeline, ["event received", "last finished", "middle read", "provider closed"]
        )

    async def test_isolated_own_provision_releases_outer_committed_dependency(self) -> None:
        """Own same-name provisions in another realm cannot replace the injected guard."""
        for action in ("consumer", "provider", "root"):
            await self._isolated_own_provision(action)

    async def _isolated_own_provision(self, action: str) -> None:
        """Exercise the source's three retirement orders without a stale dependent guard."""
        scope = self.root.isolate("foo")

        async def provide(context: Context, config: JsonValue) -> None:
            """Provide an integer used by an independently isolated consumer provision."""
            context.provide_as("foo", 1)

        async def consume(context: Context) -> None:
            """Retain an outer dependency while publishing the same key in a new realm."""
            value = context.get_as("foo", int).arc()
            context.isolate("foo").provide_as("foo", value + 1)

        provider = scope.plugin_def(PluginDef.new(provide))
        await provider.settled()
        consumer = scope.inject(["foo"], consume)
        await consumer.settled()
        if action == "root":
            await self.root.fiber().dispose()
        elif action == "provider":
            await provider.dispose()
        else:
            await consumer.dispose()
            await provider.dispose()
        self.assertEqual(scope.bindings("foo"), [])

    async def test_composed_provider_inverses_wait_for_consumers(self) -> None:
        """Adopted binding withdrawal completes before closing its enclosing resource."""
        for owner_shutdown in (False, True):
            await self._composed_provider(owner_shutdown)

    async def _composed_provider(self, owner_shutdown: bool) -> None:
        """Dispose a composed provider directly or through its owning parent fiber."""
        owner = self.root.plugin_def(PluginDef.new(noop))
        await owner.settled()
        fx = Fixture(owner.context())

        async def provide(context: Context, config: JsonValue) -> None:
            """Compose the service and its resource closure into a sequential inverse tree."""

            def compose(scope: EffectScope) -> None:
                """Place service withdrawal before resource closure in reverse acquisition order."""

                def close() -> None:
                    """Close after the binding inverse has released all consumer guards."""
                    fx.resource.closed = True
                    fx.timeline.append("provider closed")

                scope.adopt(context.on_dispose(close))
                scope.adopt(context.provide_as("foo", fx.resource))

            context.effect_sync("composed", compose)

        provider = fx.root.plugin_def(PluginDef.new(provide))
        await provider.settled()
        await fx.consumer()
        pending = owner.dispose() if owner_shutdown else provider.dispose()
        await fx.draining.wait()
        self.assertFalse(fx.resource.closed)
        fx.gate.set()
        await pending
        self.assertEqual(fx.timeline, ["consumer read: open", "provider closed"])

        await owner.dispose()

    async def test_same_name_provisions_in_distinct_realms_keep_both_guards(self) -> None:
        """Port same-name provisions on one provider under both provider and root disposal."""
        for root_shutdown in (False, True):
            await self._separate_realms(root_shutdown)

    async def _separate_realms(self, root_shutdown: bool) -> None:
        """Hold one realm's consumer without allowing another same-name binding to hide it."""
        from pykardia import RealmId

        first, second = RealmId.new(), RealmId.new()
        gate, draining = asyncio.Event(), asyncio.Event()
        closed: list[bool] = []
        read: list[int] = []

        async def provide(context: Context, config: JsonValue) -> None:
            """Publish two independent bindings owned by a single fiber."""
            context.isolate_in("foo", first).provide_as("foo", 1)
            context.isolate_in("foo", second).provide_as("foo", 2)
            context.on_dispose(lambda: closed.append(True))

        async def consume(context: Context) -> None:
            """Resolve the first realm again during delayed cleanup."""

            async def cleanup() -> None:
                """Retain this committed identity until the explicit gate opens."""
                draining.set()
                await gate.wait()
                read.append(context.get_as("foo", int).arc())

            context.defer("cleanup", Disposer.future(cleanup))

        provider = self.root.plugin_def(PluginDef.new(provide))
        await provider.settled()
        consumer = self.root.isolate_in("foo", first).inject(["foo"], consume)
        await consumer.settled()
        pending = self.root.fiber().dispose() if root_shutdown else provider.dispose()
        await draining.wait()
        self.assertEqual(closed, [])
        gate.set()
        await consumer.dispose()
        await pending
        self.assertEqual((closed, read), ([True], [1]))
