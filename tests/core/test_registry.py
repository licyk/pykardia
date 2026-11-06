"""Registry order, replacement forwarding and inactive-context behavior."""

import asyncio
import unittest

import async_timeout

from pykardia import (
    Context,
    FiberState,
    InactiveEffect,
    JsonValue,
    PluginDef,
    Replaced,
    ValidationError,
)
from tests.core.test_fiber import noop, recording


class RegistryTests(unittest.IsolatedAsyncioTestCase):
    """Exercise replacement through real fibers and their service consumers."""

    async def asyncSetUp(self) -> None:
        """Create an isolated root with a finite asynchronous deadline."""
        self.root = Context()
        self.timeout = async_timeout.timeout(5)
        await self.timeout.__aenter__()

    async def asyncTearDown(self) -> None:
        """Revert remaining definitions and resources after each scenario."""
        if not self.timeout.expired:
            await self.root.fiber().dispose()
        await self.timeout.__aexit__(None, None, None)

    async def test_replacement_preserves_ids_config_and_registration_order(self) -> None:
        """Replace multiple fibers in place and emit one event for each moved fiber."""
        disposed: list[JsonValue] = []
        applied: list[JsonValue] = []
        events: list[object] = []

        async def apply(context: Context, config: JsonValue) -> None:
            """Collect one inverse for every old implementation episode."""
            context.on_dispose(lambda: disposed.append(config))

        old, new = PluginDef.new(apply), recording(applied)
        first, second = self.root.plugin_def(old, "first"), self.root.plugin_def(old, "second")
        await asyncio.gather(first.settled(), second.settled())
        self.root.on(Replaced, lambda event: events.append(event.fiber.runtime()))
        identities = [first.id(), second.id()]
        moved = self.root.registry().replace(old, new)
        self.assertEqual(moved, [first, second])
        self.assertEqual(events, [new.key(), new.key()])
        await asyncio.gather(*(fiber.settled() for fiber in moved))
        self.assertEqual([fiber.id() for fiber in moved], identities)
        self.assertEqual(applied, ["first", "second"])
        self.assertEqual(disposed, ["first", "second"])
        self.assertEqual(self.root.registry().keys(), [new.key()])
        self.assertEqual(self.root.registry().fibers(old), moved)
        await asyncio.gather(first.dispose(), second.dispose())
        self.assertEqual(self.root.registry().size(), 0)
        self.assertFalse(self.root.registry().has(new))

    async def test_merge_into_registered_definition_does_not_restart_existing(self) -> None:
        """Moved fibers append after existing instances without reloading those instances."""
        old_calls: list[JsonValue] = []
        new_calls: list[JsonValue] = []
        old, new = recording(old_calls), recording(new_calls)
        first, second = self.root.plugin_def(old, "a"), self.root.plugin_def(new, "b")
        await asyncio.gather(first.settled(), second.settled())
        self.root.registry().replace(old, new)
        await first.settled()
        self.assertEqual(self.root.registry().fibers(new), [second, first])
        self.assertEqual(new_calls, ["b", "a"])
        self.assertEqual(first.runtime(), second.runtime())

    async def test_alias_chain_switch_back_and_unregistered_forwarding(self) -> None:
        """Aliases survive absent registrations and can be reversed without creating a loop."""
        first_calls: list[JsonValue] = []
        second_calls: list[JsonValue] = []
        third_calls: list[JsonValue] = []
        first, second, third = (
            recording(first_calls),
            recording(second_calls),
            recording(third_calls),
        )
        self.assertEqual(self.root.registry().replace(first, second), [])
        self.assertEqual(self.root.registry().size(), 0)
        fiber = self.root.plugin_def(first, 1)
        await fiber.settled()
        self.assertEqual((first_calls, second_calls), ([], [1]))
        self.root.registry().replace(first, third)
        await fiber.settled()
        later = self.root.plugin_def(first, 2)
        await later.settled()
        self.assertEqual(third_calls, [1, 2])
        self.root.registry().replace(third, first)
        await asyncio.gather(fiber.settled(), later.settled())
        self.assertEqual(self.root.registry().keys(), [first.key()])
        self.assertEqual(first_calls, [1, 2])
        self.assertEqual(self.root.registry().get(second), first.key())

    async def test_replacement_re_resolves_new_dependencies(self) -> None:
        """Added dependencies suspend a fiber; dropped dependencies release old guards."""
        calls: list[JsonValue] = []
        old = PluginDef.new(noop)
        new = recording(calls).inject(["dep"])
        fiber = self.root.plugin_def(old)
        await fiber.settled()
        self.root.registry().replace(old, new)
        await fiber.settled()
        self.assertEqual(fiber.state(), FiberState.Pending)
        self.assertEqual(list(fiber.inject().keys()), ["dep"])
        binding = self.root.provide_as("dep", 1)
        await fiber.settled()
        self.assertEqual(calls, [None])
        independent = recording(calls)
        self.root.registry().replace(new, independent)
        await fiber.settled()
        await binding.dispose()
        self.assertEqual(calls, [None, None])
        self.assertEqual(fiber.state(), FiberState.Active)
        for dependencies in ([], ["available"]):
            original = PluginDef.new(noop).inject(["missing"])
            pending = self.root.plugin_def(original)
            if dependencies:
                self.root.provide_as("available", None)
            replacement = recording(calls).inject(dependencies)
            self.root.registry().replace(original, replacement)
            await pending.settled()
            self.assertEqual(pending.state(), FiberState.Active)

    async def test_new_schema_failure_and_recovery(self) -> None:
        """Replacement decodes original source through the new schema and clears old errors."""
        applied: list[JsonValue] = []
        old = PluginDef.new(noop)
        fiber = self.root.plugin_def(old, {"foo": 1})
        await fiber.settled()

        def reject(value: JsonValue) -> JsonValue:
            """Reject only through the replacement's real decoder chain."""
            raise ValidationError.message("new schema")

        invalid = recording(applied).validate_with(reject)
        self.root.registry().replace(old, invalid)
        with self.assertRaisesRegex(ValidationError, "new schema"):
            await fiber.settled()
        self.assertEqual(fiber.state(), FiberState.Failed)

        def version(value: JsonValue) -> JsonValue:
            """Transform the source for the replacement apply body."""
            assert isinstance(value, dict)
            return {**value, "version": 2}

        valid = recording(applied).validate_with(version)
        self.root.registry().replace(invalid, valid)
        await fiber.settled()
        self.assertEqual(applied, [{"foo": 1, "version": 2}])
        self.assertEqual(fiber.config(), {"foo": 1})

        async def fail(context: Context, config: JsonValue) -> None:
            """Reject during apply after successful config resolution."""
            raise RuntimeError("replacement apply failed")

        self.root.registry().replace(valid, PluginDef.new(fail))
        with self.assertRaisesRegex(RuntimeError, "replacement apply failed"):
            await fiber.settled()
        self.assertEqual(fiber.state(), FiberState.Failed)

    async def test_replaced_provider_reloads_consumers(self) -> None:
        """A provider's new implementation changes the binding and restarts its consumers."""

        def provider(value: str) -> PluginDef:
            """Build a provider with a captured version."""

            async def apply(context: Context, config: JsonValue) -> None:
                """Provide the implementation-specific value."""
                context.provide_as("replaced", value)

            return PluginDef.new(apply)

        values: list[str] = []

        async def consume(context: Context) -> None:
            """Read the committed value on each consumer episode."""
            values.append(context.get_as("replaced", str).arc())

        old, new = provider("old"), provider("new")
        fiber = self.root.plugin_def(old)
        await fiber.settled()
        consumer = self.root.inject(["replaced"], consume)
        await consumer.settled()
        self.root.registry().replace(old, new)
        await fiber.settled()
        await consumer.settled()
        self.assertEqual(values, ["old", "new"])

    async def test_same_key_builder_retains_registered_definition(self) -> None:
        """Preserve the pinned source's entry-or-insert behavior for same-key builders."""
        applied: list[JsonValue] = []
        old = recording(applied)
        fiber = self.root.plugin_def(old, 1)
        await fiber.settled()
        builder = old.name("renamed").validate_with(lambda value: 99)
        self.root.registry().replace(old, builder)
        await fiber.settled()
        self.assertIs(fiber.def_(), old)
        self.assertEqual(applied, [1, 1])
        self.assertEqual(fiber.name(), "root")

    async def test_nested_resources_restore_registry_and_reject_stale_context(self) -> None:
        """Parent retirement reverts nested registrations and rejects stale effect creation."""

        async def apply(context: Context, config: JsonValue) -> None:
            """Create a provider and a nested dependent instantiation."""
            context.provide_as("foo", 1)
            context.plugin_def(PluginDef.new(noop).inject(["foo"]))

        initial = (self.root.registry().size(), self.root.fiber().effect_count())
        fiber = self.root.plugin_def(PluginDef.new(apply))
        await fiber.settled()
        self.assertEqual((self.root.registry().size(), self.root.fiber().effect_count()), (2, 1))
        await fiber.dispose()
        self.assertEqual((self.root.registry().size(), self.root.fiber().effect_count()), initial)
        stale = fiber.context()
        with self.assertRaises(InactiveEffect):
            stale.plugin_def(PluginDef.new(noop))
        with self.assertRaises(InactiveEffect):
            stale.provide_as("foo", 2)
        with self.assertRaises(InactiveEffect):
            stale.on_dispose(lambda: None)
        with self.assertRaises(InactiveEffect):
            fiber.update(2)
        with self.assertRaises(InactiveEffect):
            await fiber.restart()
        self.assertEqual(self.root.bindings("foo"), [])

    async def test_fiber_dispose_is_once_and_rejects_effects_during_retired_cleanup(self) -> None:
        """Check effect introspection and inactive-context rejection inside the actual inverse."""
        from pykardia import EffectMeta, Event

        class Ping(Event[None]):
            """A listener registration attempted through a retired context."""

        calls: list[str] = []

        async def apply(context: Context, config: JsonValue) -> None:
            """Collect a cleanup callback that probes each effect registration boundary."""

            def cleanup() -> None:
                """Reject new resources while the removed fiber is still Unloading."""
                calls.append("disposed")
                with self.assertRaises(InactiveEffect):
                    context.plugin_def(PluginDef.new(noop))
                with self.assertRaises(InactiveEffect):
                    context.on_dispose(lambda: None)
                with self.assertRaises(InactiveEffect):
                    context.on(Ping, lambda event: None)

            context.effect_sync("test", lambda scope: scope.defer_fn(cleanup))

        fiber = self.root.plugin_def(PluginDef.new(apply))
        await fiber.settled()
        self.assertEqual(fiber.effects(), [EffectMeta("test", [])])
        await fiber.dispose()
        await fiber.dispose()
        self.assertEqual(calls, ["disposed"])
