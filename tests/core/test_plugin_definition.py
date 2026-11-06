"""Plugin definition identity, copy boundaries and decoder behavior."""

import unittest
from dataclasses import dataclass

from pykardia import Context, Inject, JsonValue, Plugin, PluginDef


@dataclass
class Settings:
    """A typed configuration whose decoded identity can be observed."""

    count: int = 0


class Counter(Plugin[Settings]):
    """A static plugin used to observe stable type identity and typed decoding."""

    NAME = "Counter"
    Config = Settings
    seen: list[Settings] = []

    @staticmethod
    async def apply(ctx: Context, config: Settings) -> None:
        """Record the exact decoded configuration instance."""
        Counter.seen.append(config)


class PluginDefinitionTests(unittest.IsolatedAsyncioTestCase):
    """Execute real definitions without substituting their decoder or apply wrappers."""

    async def test_type_identity_and_decoded_configuration(self) -> None:
        """Static definitions share a key and pass their decoded object directly."""
        first = PluginDef.of(Counter)
        second = PluginDef.of(Counter)
        self.assertEqual(first.key(), second.key())
        config = first.decode({"count": 3, "unknown": True})
        self.assertEqual(config, Settings(3))
        await first.apply(Context(), config)
        self.assertIs(Counter.seen[-1], config)
        self.assertEqual(first.decode(None), Settings())

    async def test_closure_keys_builders_and_json_clones(self) -> None:
        """Closure construction allocates identity; builders preserve it and snapshot data."""
        seen: list[JsonValue] = []

        async def apply(ctx: Context, config: JsonValue) -> None:
            """Mutate a raw apply argument to expose accidental shared JSON containers."""
            seen.append(config)
            assert isinstance(config, dict)
            config["changed"] = True

        first = PluginDef.new(apply)
        self.assertNotEqual(first.key(), PluginDef.new(apply).key())
        derived = first.name("named").inject(["a", "b"]).provides(["c"]).tag("guest")
        self.assertEqual(derived.key(), first.key())
        self.assertIsNone(first.plugin_name())
        self.assertTrue(first.inject_spec().is_empty())
        self.assertEqual(derived.plugin_name(), "named")
        self.assertEqual(list(derived.inject_spec().keys()), ["a", "b"])
        self.assertEqual(derived.provide_spec(), ("c",))
        self.assertTrue(derived.has_tag("guest"))
        source: JsonValue = {"count": 2}
        decoded = derived.decode(source)
        await derived.apply(Context(), decoded)
        await derived.apply(Context(), decoded)
        self.assertEqual(decoded, source)
        self.assertIsNot(seen[0], seen[1])

    async def test_typed_closure_and_validator_order(self) -> None:
        """Latest raw validators run first; a typed apply receives one decoded instance."""
        trace: list[str] = []
        seen: list[Settings] = []

        def decoder(value: JsonValue) -> Settings:
            """Decode after both validators have transformed the raw JSON."""
            trace.append("decode")
            assert isinstance(value, int)
            return Settings(value)

        def first(value: JsonValue) -> JsonValue:
            """Observe the result of the most recently installed validator."""
            trace.append("first")
            assert isinstance(value, int)
            return value * 2

        def last(value: JsonValue) -> JsonValue:
            """Apply the outermost transformation first."""
            trace.append("last")
            assert isinstance(value, int)
            return value + 1

        async def apply(ctx: Context, config: Settings) -> None:
            """Retain the decoded typed object."""
            seen.append(config)

        definition = PluginDef.typed(apply, decoder).validate_with(first).validate_with(last)
        config = definition.decode(2)
        await definition.apply(Context(), config)
        self.assertEqual(trace, ["last", "first", "decode"])
        self.assertEqual(config, Settings(6))
        self.assertIs(seen[0], config)

    def test_order_metadata_presence_and_json_equality(self) -> None:
        """Replacement retains insertion order and explicit null differs from absent metadata."""
        original = Inject(["b", "a", "b"])
        derived = original.with_meta("b", {"x": 1}).with_meta("c", None)
        self.assertEqual(list(derived.keys()), ["b", "a", "c"])
        self.assertTrue(derived.has_meta("c"))
        self.assertFalse(original.has_meta("b"))
        self.assertNotEqual(derived, derived.with_("c"))
        self.assertEqual(Inject(["a", "b"]), original)
        self.assertNotEqual(Inject().with_meta("x", True), Inject().with_meta("x", 1))
        self.assertNotEqual(Inject().with_meta("x", 1.0), Inject().with_meta("x", 1))
        metadata = derived.meta("b")
        assert isinstance(metadata, dict)
        metadata["x"] = 2
        self.assertEqual(derived.meta("b"), {"x": 1})

    async def test_typed_plugin_source_serialization_preserves_aliases(self) -> None:
        """Serialize typed config field aliases before the normal plugin decoder reads it."""
        from dataclasses import field

        @dataclass
        class Aliased:
            """A typed source with a public camelCase field name."""

            initial_value: int = field(metadata={"alias": "initialValue"})

        class Typed(Plugin[Aliased]):
            """Observe real typed plugin instantiation rather than invoking apply directly."""

            NAME = "typed"
            Config = Aliased
            values: list[int] = []

            @staticmethod
            async def apply(ctx: Context, config: Aliased) -> None:
                """Record the actual decoded field value."""
                Typed.values.append(config.initial_value)

        root = Context()
        fiber = root.plugin(Typed, Aliased(7))
        await fiber.settled()
        self.assertEqual(Typed.values, [7])
        self.assertEqual(fiber.config(), {"initialValue": 7})
        self.assertEqual(fiber.name(), "typed")
        await root.fiber().dispose()
