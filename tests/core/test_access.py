"""Checked service access, isolation, JSON views and metadata parity scenarios."""

import asyncio
import unittest
from dataclasses import dataclass

import async_timeout

from pykardia import (
    AlreadyProvided,
    Context,
    FiberState,
    Inactive,
    Inject,
    JsonValue,
    Overlay,
    PluginDef,
    RealmId,
    Service,
    ServiceChanged,
    TypeMismatch,
    Undeclared,
)


@dataclass
class Counter(Service):
    """A mutable service whose concrete object and serialized view can be observed."""

    NAME = "counter"
    count: int


class AccessTests(unittest.IsolatedAsyncioTestCase):
    """Run real providers and consumers against the actual store and scope chain."""

    async def asyncSetUp(self) -> None:
        """Create an independent runtime and a finite asynchronous deadline."""
        self.root = Context()
        self.timeout = async_timeout.timeout(5)
        await self.timeout.__aenter__()

    async def asyncTearDown(self) -> None:
        """Release all actual fibers and bindings, then close the scenario deadline."""
        if not self.timeout.expired:
            await self.root.fiber().dispose()
        await self.timeout.__aexit__(None, None, None)

    async def test_typed_name_access_and_json_views(self) -> None:
        """Concrete downcasts preserve identity, while serialized views follow mutations."""
        value = Counter(1)
        handle = self.root.provide(value)
        self.assertIs(self.root.get(Counter).arc(), value)
        with self.assertRaises(TypeMismatch):
            self.root.view("counter")
        with self.assertRaises(TypeMismatch):
            self.root.get_as("counter", int)
        self.assertIsNone(self.root.live_as("counter", int))
        await handle.dispose()
        self.root.provide_serialized_as("counter", value)
        self.assertEqual(self.root.view("counter"), {"count": 1})
        value.count = 2
        self.assertEqual(self.root.view("counter"), {"count": 2})
        data: JsonValue = {"x": [1]}
        self.root.provide_as("json", data)
        snapshot = self.root.view("json")
        assert isinstance(snapshot, dict)
        snapshot["x"] = 3
        self.assertEqual(self.root.view("json"), data)
        self.root.provide_as("boolean", True)
        with self.assertRaises(TypeMismatch):
            self.root.get_as("boolean", int)

    async def test_access_check_duplicate_and_disposed_context(self) -> None:
        """Undeclared access and duplicate slots fail; collected contexts remain inactive."""

        async def apply(context: Context, config: JsonValue) -> None:
            """Exercise actual get/provide errors from a running named plugin."""
            with self.assertRaises(Undeclared):
                context.get_as("bar", int)
            context.provide_as("foo", 0)
            self.assertEqual(context.get_as("foo", int).arc(), 0)
            with self.assertRaisesRegex(AlreadyProvided, "<named>"):
                context.provide_as("foo", 1)

        fiber = self.root.plugin_def(PluginDef.new(apply).name("named"))
        await fiber.settled()
        self.assertEqual(repr(fiber.context()), "Context <named>")
        await fiber.dispose()
        with self.assertRaises(Inactive):
            fiber.context().get_as("foo", int)

    async def test_own_provisions_do_not_cross_isolation_boundaries(self) -> None:
        """Port own provision access through same-realm children and isolated siblings."""
        observations: list[str] = []

        def observe(context: Context) -> None:
            """Record a string service or the exact undeclared diagnostic."""
            try:
                observations.append(context.get_as("foo", str).arc())
            except Undeclared as error:
                observations.append(str(error))

        async def child(context: Context, config: JsonValue) -> None:
            """Use the child's own scope to resolve inherited provisions."""
            observe(context)

        async def apply(context: Context, config: JsonValue) -> None:
            """Provide in one derived realm and probe both derivation chains."""
            isolated = context.isolate("foo")
            isolated.provide_as("foo", "own")
            observe(isolated)
            observe(context)
            await isolated.plugin_def(PluginDef.new(child)).settled()
            await context.plugin_def(PluginDef.new(child)).settled()

        await self.root.plugin_def(PluginDef.new(apply)).settled()
        undeclared = 'cannot get property "foo" without inject'
        self.assertEqual(observations, ["own", undeclared, "own", undeclared])

    async def test_injected_view_stops_even_at_same_realm_isolation(self) -> None:
        """Pinning the same realm is still an isolation boundary for inherited declarations."""
        self.root.provide_as("foo", "outer")
        observations: list[str] = []

        async def child(context: Context, config: JsonValue) -> None:
            """Read through the nearest declaration or record an isolation rejection."""
            try:
                observations.append(context.get_as("foo", str).arc())
            except Undeclared:
                observations.append("undeclared")

        async def apply(context: Context) -> None:
            """Compare ordinary derivation, fresh isolation and explicit same-realm isolation."""
            realm = context.realm("foo")
            assert realm is not None
            for derived in (context, context.isolate("foo"), context.isolate_in("foo", realm)):
                await derived.plugin_def(PluginDef.new(child)).settled()

        await self.root.inject(["foo"], apply).settled()
        self.assertEqual(observations, ["outer", "undeclared", "undeclared"])

    async def test_own_provisions_separate_from_injected_view(self) -> None:
        """Multiple same-name own provisions never overwrite the committed outer dependency."""
        self.root.provide_as("foo", "outer")
        observed: list[str] = []

        async def apply(context: Context) -> None:
            """Withdraw one isolated provision while keeping the others and outer view usable."""
            first, second = context.isolate("foo"), context.isolate("foo")
            handle = first.provide_as("foo", "first")
            second.provide_as("foo", "second")
            observed.extend(item.get_as("foo", str).arc() for item in (context, first, second))
            await handle.dispose()
            with self.assertRaises(Undeclared):
                first.get_as("foo", str)
            self.assertEqual(second.get_as("foo", str).arc(), "second")
            context.on_dispose(lambda: observed.append(context.get_as("foo", str).arc()))

        fiber = self.root.inject(["foo"], apply)
        await fiber.settled()
        await fiber.dispose()
        self.assertEqual(observed, ["outer", "first", "second", "outer"])
        self.assertEqual(self.root.log_buffer(), [])

    async def test_shared_realm_and_service_event_filtering(self) -> None:
        """Only consumers and listeners in a changed realm react to its provision."""
        realm = RealmId.new()
        first = self.root.isolate_in("foo", realm)
        second = self.root.isolate_in("foo", realm)
        private = self.root.isolate("foo")
        applied: list[str] = []
        disposed: list[str] = []
        events: list[str] = []
        self.root.on(ServiceChanged, lambda event: events.append("root"))
        first.on(ServiceChanged, lambda event: events.append("shared"))
        private.on(ServiceChanged, lambda event: events.append("private"))

        async def apply(context: Context, config: JsonValue) -> None:
            """Record active episode boundaries for a labeled realm consumer."""
            assert isinstance(config, str)
            applied.append(config)
            context.on_dispose(lambda: disposed.append(config))

        definition = PluginDef.new(apply).inject(["foo"])
        fibers = [
            scope.plugin_def(definition, label)
            for scope, label in [
                (self.root, "root"),
                (first, "first"),
                (second, "second"),
                (private, "private"),
            ]
        ]
        binding = first.provide_as("foo", 200)
        await asyncio.gather(*(fiber.settled() for fiber in fibers))
        self.assertEqual(applied, ["first", "second"])
        self.assertEqual(events, ["shared"])
        self.assertEqual(second.get_as("foo", int).arc(), 200)
        self.assertIsNone(private.live_as("foo", int))
        await binding.dispose()
        self.assertEqual(disposed, ["first", "second"])
        self.assertEqual(events, ["shared", "shared"])

    async def test_loading_provider_is_not_live_or_available_to_consumers(self) -> None:
        """Root checked access can see a loading provision, while strict live access cannot."""
        started, release = asyncio.Event(), asyncio.Event()
        calls: list[int] = []

        async def provide(context: Context, config: JsonValue) -> None:
            """Acquire a binding before asynchronous provider initialization finishes."""
            context.provide_as("foo", 1)
            started.set()
            await release.wait()

        async def consume(context: Context) -> None:
            """Start only after provider activation."""
            calls.append(context.get_as("foo", int).arc())

        consumer = self.root.inject(["foo"], consume)
        provider = self.root.plugin_def(PluginDef.new(provide))
        await started.wait()
        self.assertEqual(self.root.get_as("foo", int).arc(), 1)
        self.assertIsNone(self.root.live_as("foo", int))
        self.assertEqual((calls, consumer.state()), ([], FiberState.Pending))
        release.set()
        await provider.settled()
        await consumer.settled()
        self.assertEqual(calls, [1])

    async def test_checked_metadata_and_refresh(self) -> None:
        """Availability uses inject metadata before context metadata and refreshes explicitly."""
        seen: list[list[JsonValue]] = []
        calls: list[int] = []
        overlay = Overlay()
        overlay.set_intercepts({"foo": {"allowed": False}})
        scope = self.root.extend_with(overlay)

        def check(metadata: list[JsonValue]) -> bool:
            """Observe the actual metadata sequence without reentering the state lock."""
            seen.append(metadata)
            last = metadata[-1]
            return isinstance(last, dict) and last.get("allowed") is True

        async def consume(context: Context) -> None:
            """Read the admitted binding once the availability check permits it."""
            calls.append(context.get_as("foo", int).arc())

        self.root.provide_checked("foo", 7, check)
        fiber = scope.inject(Inject().with_meta("foo", None), consume)
        self.assertEqual(fiber.state(), FiberState.Pending)
        self.assertEqual(seen, [[None, {"allowed": False}]])
        overlay.set_intercepts({"foo": {"allowed": True}})
        realm = scope.realm("foo")
        assert realm is not None
        scope.refresh_consumers("foo", realm)
        await fiber.settled()
        self.assertEqual(calls, [7])
        self.assertEqual(seen[-1], [None, {"allowed": True}])

    async def test_binding_migration_preserves_committed_identity(self) -> None:
        """Moving a binding and scope together preserves its ID and avoids consumer restart."""
        overlay = Overlay()
        first, second = RealmId.new(), RealmId.new()
        overlay.set_isolations({"foo": first})
        scope = self.root.extend_with(overlay)
        scope.provide_as("foo", 4)
        calls: list[int] = []

        async def consume(context: Context) -> None:
            """Count actual episode starts while retaining the binding identity."""
            calls.append(context.get_as("foo", int).arc())

        fiber = scope.inject(["foo"], consume)
        await fiber.settled()
        identity = fiber.context().get_as("foo", int).impl_id()
        self.assertTrue(scope.migrate_binding("foo", first, second))
        overlay.set_isolations({"foo": second})
        scope.refresh_consumers("foo", second)
        await fiber.settled()
        self.assertEqual(calls, [4])
        self.assertEqual(scope.bindings("foo"), [(second, identity)])
        provider = scope.binding_provider("foo", second)
        assert provider is not None
        self.assertTrue(provider.ptr_eq(scope))
        self.assertIsNone(scope.binding_provider("wrong", second))
        self.assertFalse(scope.migrate_binding("wrong", second, first))
        self.assertFalse(scope.migrate_binding("foo", second, second))

    async def test_multiple_injects_start_in_dependency_order(self) -> None:
        """Port the explicit qux -> foo -> bar ordering with two dependencies on bar."""
        started: list[str] = []

        def definition(name: str, dependencies: list[str]) -> PluginDef:
            """Declare a real named provider with the supplied dependency tuple."""

            async def apply(context: Context, config: JsonValue) -> None:
                """Publish a service and observe actual apply order."""
                context.provide_as(name, None)
                started.append(name)

            return PluginDef.new(apply).inject(dependencies)

        foo = self.root.plugin_def(definition("foo", ["qux"]))
        bar = self.root.plugin_def(definition("bar", ["foo", "qux"]))
        qux = self.root.plugin_def(definition("qux", []))
        await qux.settled()
        await foo.settled()
        await bar.settled()
        self.assertEqual(started, ["qux", "foo", "bar"])

    async def test_isolated_contexts_activate_and_cleanup_independently(self) -> None:
        """Port three isolated consumer contexts with independent provision and withdrawal."""
        scopes = [self.root, self.root.isolate("foo"), self.root.isolate("foo")]
        applied: list[JsonValue] = []
        disposed: list[JsonValue] = []

        async def apply(context: Context, config: JsonValue) -> None:
            """Observe starts and inverse completion in the selected realm only."""
            applied.append(config)
            context.on_dispose(lambda: disposed.append(config))

        definition = PluginDef.new(apply).inject(["foo"])
        fibers = [scope.plugin_def(definition, index) for index, scope in enumerate(scopes)]
        first = scopes[0].provide_as("foo", 100)
        await asyncio.gather(*(fiber.settled() for fiber in fibers))
        self.assertEqual((applied, disposed), ([0], []))
        scopes[1].provide_as("foo", 200)
        await fibers[1].settled()
        self.assertEqual((applied, disposed), ([0, 1], []))
        await first.dispose()
        self.assertEqual((applied, disposed), ([0, 1], [0]))
        scopes[2].provide_as("foo", 300)
        await fibers[2].settled()
        self.assertEqual((applied, disposed), ([0, 1, 2], [0]))
        self.assertIsNone(scopes[0].live_as("foo", int))
        self.assertEqual(scopes[1].get_as("foo", int).arc(), 200)
        self.assertEqual(scopes[2].get_as("foo", int).arc(), 300)
