"""Metadata, service definition/use sites and associated service name parity."""

from __future__ import annotations

import unittest
from collections.abc import Iterable
from dataclasses import dataclass

import async_timeout

from pykardia import (
    Context,
    EffectHandle,
    EffectScope,
    Inactive,
    Inject,
    JsonValue,
    PluginDef,
    RealmId,
    Service,
    Svc,
    Undeclared,
)


class Foo(Service):
    """A service used to observe admitted interception metadata."""

    NAME = "foo"


class Tagged(Service):
    """A service selecting ordered tag concatenation instead of shallow merge."""

    NAME = "tagged"

    @staticmethod
    def merge_intercept(values: Iterable[JsonValue]) -> JsonValue:
        """Concatenate tag arrays in declared and context interception order."""
        tags: list[JsonValue] = []
        for value in values:
            if isinstance(value, dict) and isinstance(value.get("tags"), list):
                items = value["tags"]
                assert isinstance(items, list)
                tags.extend(items)
        return {"tags": tags}


@dataclass
class Outer(Service):
    """A service retaining its construction context for its own dependency access."""

    NAME = "outer"
    context: Context

    def create(self) -> Holder:
        """Return a plain captured value without propagating a caller scope."""
        return Holder(self.context)


@dataclass
class Holder:
    """A plain service-created value whose later reads use the captured definition site."""

    context: Context

    def probe(self) -> tuple[Outer, Foo]:
        """Read an own provision and a dependency through the retained context."""
        return self.context.get(Outer).arc(), self.context.get(Foo).arc()


@dataclass
class Counter(Service):
    """A mutable resource whose changes are attributed explicitly to service callers."""

    NAME = "counter"
    value: int = 0


def increase(service: Svc[Counter]) -> EffectHandle:
    """Acquire an increment on the use-site fiber and revert it when that fiber closes."""
    counter = service.arc()

    def effect(scope: EffectScope) -> None:
        """Collect a reversible mutation on the actual shared service value."""
        counter.value += 1

        def inverse() -> None:
            """Revert precisely this invocation's increment."""
            counter.value -= 1

        scope.defer_fn(inverse)

    return service.caller().effect_sync("counter.increase()", effect)


class Associated(Service):
    """A dotted coeffect key with no implicit relation to its prefix."""

    NAME = "foo.bar"


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    """Check use-site effects without rebinding methods or proxying definition-site context."""

    async def asyncSetUp(self) -> None:
        """Create an isolated root and bound asynchronous scenarios."""
        self.root = Context()
        self.timeout = async_timeout.timeout(5)
        await self.timeout.__aenter__()

    async def asyncTearDown(self) -> None:
        """Dispose the root's actual resources after each scenario."""
        if not self.timeout.expired:
            await self.root.fiber().dispose()
        await self.timeout.__aexit__(None, None, None)

    async def test_metadata_merges_inject_then_outer_to_inner_context(self) -> None:
        """Context metadata overrides declarations; custom mergers receive the full sequence."""
        self.root.provide(Foo())
        self.root.provide(Tagged())
        observed: list[JsonValue] = []
        scope = self.root.intercept("foo", {"b": 2}).intercept("tagged", {"tags": ["outer"]})

        async def consume(context: Context) -> None:
            """Read both default and custom metadata through actual service handles."""
            observed.append(context.intercept("foo", {"c": 3}).get(Foo).meta())
            observed.append(context.intercept("tagged", {"tags": ["inner"]}).get(Tagged).meta())

        specification = (
            Inject().with_meta("foo", {"a": 1, "b": 1}).with_meta("tagged", {"tags": ["inject"]})
        )
        await scope.inject(specification, consume).settled()
        self.assertEqual(
            observed, [{"a": 1, "b": 2, "c": 3}, {"tags": ["inject", "outer", "inner"]}]
        )

    async def test_nearest_declaring_fiber_and_own_provision_metadata(self) -> None:
        """Nearest declarations supply metadata; own provisions omit injected metadata."""

        async def provide(context: Context, config: JsonValue) -> None:
            """Provide foo from a distinct plugin so accesses require injection."""
            context.provide(Foo())

        await self.root.plugin_def(PluginDef.new(provide)).settled()
        observed: list[JsonValue] = []

        async def child(context: Context) -> None:
            """Observe the nearest declaration that admits this service access."""
            observed.append(context.get(Foo).meta())

        async def parent(context: Context) -> None:
            """Compare inherited and own declarations plus same-name own provisions."""
            await context.inject([], child).settled()
            await context.inject(Inject().with_meta("foo", {"b": 2}), child).settled()
            isolated = context.isolate("foo")
            isolated.provide(Foo())
            observed.append(context.get(Foo).meta())
            observed.append(isolated.get(Foo).meta())
            observed.append(isolated.intercept("foo", {"c": 3}).get(Foo).meta())

        await self.root.inject(Inject().with_meta("foo", {"a": 1}), parent).settled()
        self.assertEqual(observed, [{"a": 1}, {"b": 2}, {"a": 1}, {}, {"c": 3}])

    async def test_live_metadata_stops_at_isolation_boundary(self) -> None:
        """Reflective live access does not inherit admission metadata across re-isolation."""
        self.root.provide(Foo())
        isolated = self.root.isolate("foo")
        isolated.provide(Foo())
        realm = isolated.realm("foo")
        assert realm is not None
        observed: list[JsonValue] = []

        async def consume(context: Context) -> None:
            """Read the isolated binding reflectively while keeping declared metadata outside."""
            service = context.isolate_in("foo", realm).live(Foo)
            assert service is not None
            observed.append(service.meta())

        await self.root.inject(Inject().with_meta("foo", {"a": 1}), consume).settled()
        self.assertEqual(observed, [{}])

    async def test_definition_and_use_sites_remain_separate(self) -> None:
        """Keep caller scopes separate from captured dependency definition sites."""

        async def provide_foo(context: Context, config: JsonValue) -> None:
            """Create the inner service independently from its eventual callers."""
            context.provide(Foo())

        async def provide_outer(context: Context, config: JsonValue) -> None:
            """Capture a context whose own inject admits foo."""
            context.provide(Outer(context))

        inner = self.root.plugin_def(PluginDef.new(provide_foo).name("foo:driver"))
        await inner.settled()
        definition = (
            PluginDef.new(provide_outer)
            .name("outer:driver")
            .inject(Inject().with_meta("foo", {"a": 2}))
        )
        outer = self.root.plugin_def(definition)
        await outer.settled()
        observed: list[bool] = []

        async def consume(context: Context) -> None:
            """Inspect actual caller, provider and captured context identities."""
            handle = context.get(Outer)
            observed.extend([handle.caller().ptr_eq(context), handle.provider() == outer])
            nested = handle.arc().context.get(Foo)
            observed.extend([nested.caller().fiber() == outer, nested.provider() == inner])
            self.assertEqual(nested.meta(), {"a": 2})
            own, dependency = handle.arc().create().probe()
            self.assertIs(own, handle.arc())
            self.assertIs(dependency, nested.arc())
            nested.logger().info("nested")
            handle.logger().info("outer")

        caller = self.root.intercept("foo", {"b": 1})
        await caller.inject(["outer"], consume).settled()
        self.assertEqual(observed, [True] * 4)
        self.assertEqual(
            [message.name for message in self.root.log_buffer()], ["foo:driver", "outer:driver"]
        )
        overridden = caller.intercept("logger", {"name": "caller-override"}).get(Outer)
        overridden.logger().info("overridden")
        self.assertEqual(self.root.log_buffer()[-1].name, "caller-override")
        self.assertEqual(overridden.arc().context.get(Foo).meta(), {"a": 2})

    async def test_associated_names_and_undeclared_def_site_access(self) -> None:
        """Dotted keys are independent and root callers cannot bypass service declarations."""

        async def provide(context: Context, config: JsonValue) -> None:
            """Provide the dotted name from an independently disposable plugin."""
            context.provide(Associated())

        async def outer(context: Context, config: JsonValue) -> None:
            """Capture a definition site declaring foo but not its associated key."""
            context.provide(Outer(context))

        self.root.provide(Foo())
        associated = self.root.plugin_def(PluginDef.new(provide))
        await associated.settled()
        self.assertIsInstance(self.root.get(Associated).arc(), Associated)

        async def admitted(context: Context) -> None:
            """Access the dotted service through its own explicit declaration."""
            self.assertIsInstance(context.get(Associated).arc(), Associated)

        await self.root.inject(["foo", "foo.bar"], admitted).settled()
        provider = self.root.plugin_def(PluginDef.new(outer).inject(["foo"]))
        await provider.settled()
        with self.assertRaises(Undeclared):
            self.root.get(Outer).arc().context.get(Associated)

        async def consume(context: Context) -> None:
            """Calling through a different fiber still uses the service's declared access."""
            with self.assertRaises(Undeclared):
                context.get(Outer).arc().context.get(Associated)

        await self.root.inject(["outer"], consume).settled()
        await associated.dispose()
        with self.assertRaises(Inactive):
            self.root.get(Associated)
        self.assertIsInstance(self.root.get(Foo).arc(), Foo)

    async def test_caller_effect_ownership_and_forwarded_def_site_effects(self) -> None:
        """Direct handle work belongs to the caller; forwarded work uses its captured def site."""
        counter = Counter()
        self.root.provide(counter)
        increase(self.root.get(Counter))

        async def consume(context: Context) -> None:
            """Acquire a caller-owned effect through an admitted service handle."""
            increase(context.get(Counter))

        fiber = self.root.inject(["counter"], consume)
        await fiber.settled()
        self.assertEqual(counter.value, 2)
        await fiber.dispose()
        self.assertEqual(counter.value, 1)

        async def outer(context: Context, config: JsonValue) -> None:
            """Capture the service definition's context without requiring caller propagation."""
            context.provide(Outer(context))

        for inject in ([], ["counter"]):
            provider = self.root.plugin_def(PluginDef.new(outer).inject(inject))
            await provider.settled()
            increase(self.root.get(Outer).arc().context.get(Counter))
            self.assertEqual(counter.value, 2)
            await provider.dispose()
            self.assertEqual(counter.value, 1)

    async def test_same_name_definition_sites_and_caller_owned_plugins(self) -> None:
        """Keep captured realms while attributing created children to the caller."""
        first, second = RealmId.new(), RealmId.new()
        self.root.isolate_in("outer", first).provide(Outer(self.root.isolate_in("foo", first)))
        self.root.isolate_in("outer", second).provide(Outer(self.root.isolate_in("foo", second)))
        caller = self.root.isolate_in("outer", first).isolate_in("foo", second)
        service = caller.get(Outer)
        self.assertEqual(service.arc().context.realm("foo"), first)
        self.assertEqual(service.caller().realm("foo"), second)

        async def consume(context: Context) -> None:
            """Instantiate a child through the handle's explicit caller context."""
            handle = context.get(Outer)

            async def child(child_context: Context, config: JsonValue) -> None:
                """Complete a real child episode for parent attribution checks."""

            fiber = handle.caller().plugin_def(PluginDef.new(child))
            await fiber.settled()
            self.assertEqual(fiber.parent(), context.fiber())

        await caller.inject(["outer"], consume).settled()

    async def test_root_provided_service_keeps_unchecked_captured_context(self) -> None:
        """A service constructed on root can read a plugin provision without declaring inject."""

        async def provide(context: Context, config: JsonValue) -> None:
            """Publish foo from a plugin rather than a root-owned direct provision."""
            context.provide(Foo())

        await self.root.plugin_def(PluginDef.new(provide)).settled()
        self.root.provide(Outer(self.root))
        self.assertIsInstance(self.root.get(Outer).arc().context.get(Foo).arc(), Foo)
