"""Configuration hook ordering, update takeover and source commit semantics."""

import asyncio
import unittest
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import async_timeout

from pykardia import Context, Fiber, JsonValue, PluginDef, ValidationError, decode


@dataclass
class Settings:
    """Decoded typed configuration used to distinguish hook acceptance types."""

    value: int


class ConfigHookTests(unittest.IsolatedAsyncioTestCase):
    """Invoke real hook chains around the production decoder and fiber update path."""

    async def asyncSetUp(self) -> None:
        """Create a root with a finite asynchronous deadline."""
        self.root = Context()
        self.timeout = async_timeout.timeout(5)
        await self.timeout.__aenter__()

    async def asyncTearDown(self) -> None:
        """Remove hooks and active fibers through their own inverses."""
        if not self.timeout.expired:
            await self.root.fiber().dispose()
        await self.timeout.__aexit__(None, None, None)

    async def test_config_hooks_wrap_decoder_outermost_first(self) -> None:
        """Hook registration order defines nesting, and removing a hook restores the chain."""
        trace: list[str] = []
        applied: list[Settings] = []

        def outer(fiber: Fiber, config: JsonValue, next_: Callable[[JsonValue], object]) -> object:
            """Transform raw source and observe the decoded return value."""
            trace.append("outer in")
            assert isinstance(config, dict)
            value = config["value"]
            assert isinstance(value, int)
            result = next_({"value": value + 1})
            self.assertIsInstance(result, Settings)
            trace.append("outer out")
            return result

        def inner(fiber: Fiber, config: JsonValue, next_: Callable[[JsonValue], object]) -> object:
            """Apply the next transformation before typed decoding."""
            trace.append("inner in")
            assert isinstance(config, dict)
            value = config["value"]
            assert isinstance(value, int)
            result = next_({"value": value * 2})
            trace.append("inner out")
            return result

        async def apply(context: Context, config: Settings) -> None:
            """Record the final typed value without modifying its source."""
            applied.append(config)

        first = self.root.on_config(outer)
        second = self.root.on_config(inner)
        definition = PluginDef.typed(apply, lambda value: decode(value, Settings))
        fiber = self.root.plugin_def(definition, {"value": 2})
        await fiber.settled()
        self.assertEqual(trace, ["outer in", "inner in", "inner out", "outer out"])
        self.assertEqual(applied, [Settings(6)])
        self.assertEqual(fiber.config(), {"value": 2})
        await second.dispose()
        await fiber.restart()
        self.assertEqual(applied[-1], Settings(3))
        await first.dispose()
        await fiber.restart()
        self.assertEqual(applied[-1], Settings(2))

    async def test_first_matching_update_hook_owns_completion(self) -> None:
        """Skip mismatched types, stop at first acceptance and await its returned operation."""
        applied: list[Settings] = []
        trace: list[str] = []
        entered, release = asyncio.Event(), asyncio.Event()

        async def accepted() -> None:
            """Hold an accepted update's own completion without restarting the episode."""
            entered.set()
            await release.wait()
            trace.append("finished")

        def accept(config: Settings) -> Awaitable[None]:
            """Accept the decoded value and return an independently observable future."""
            trace.append(f"accept {config.value}")
            return accepted()

        async def apply(context: Context, config: Settings) -> None:
            """Register mismatched, declining, accepting and unreachable hooks in order."""
            applied.append(config)
            context.on_update(str, lambda value: self.fail("wrong type offered"))
            context.on_update(Settings, lambda value: False)
            context.on_update_with(Settings, accept)
            context.on_update(Settings, lambda value: self.fail("after accepted hook"))

        fiber = self.root.plugin_def(
            PluginDef.typed(apply, lambda value: decode(value, Settings)), {"value": 1}
        )
        await fiber.settled()
        update = asyncio.ensure_future(fiber.update({"value": 2}))
        await entered.wait()
        self.assertFalse(update.done())
        self.assertEqual(fiber.config(), {"value": 2})
        self.assertEqual(applied, [Settings(1)])
        release.set()
        await update
        self.assertEqual(trace, ["accept 2", "finished"])

    async def test_removed_hooks_are_skipped_by_existing_update_snapshot(self) -> None:
        """A previous callback can remove a later captured hook before it is offered."""
        calls: list[JsonValue] = []
        missed: list[int] = []

        async def apply(context: Context, config: JsonValue) -> None:
            """Use removal from a prior hook to exercise the shared removed flag."""
            calls.append(config)

            def remove(value: int) -> bool:
                """Decline after removing the later callback from the captured snapshot."""
                later.dispose()
                return False

            context.on_update(int, remove)
            later = context.on_update(int, lambda value: bool(missed.append(value)))

        fiber = self.root.plugin_def(PluginDef.new(apply), 1)
        await fiber.settled()
        await fiber.update(2)
        self.assertEqual(calls, [1, 2])
        self.assertEqual(missed, [])

    async def test_config_hook_rejection_does_not_commit_loaded_update(self) -> None:
        """A root hook rejects before source assignment or any update callback runs."""
        applied: list[JsonValue] = []

        async def apply(context: Context, config: JsonValue) -> None:
            """Record accepted apply values."""
            applied.append(config)

        def hook(fiber: Fiber, config: JsonValue, next_: Callable[[JsonValue], object]) -> object:
            """Reject a raw value at the configuration boundary."""
            if config == "bad":
                raise ValidationError.message("blocked")
            return next_(config)

        self.root.on_config(hook)
        fiber = self.root.plugin_def(PluginDef.new(apply), "good")
        await fiber.settled()
        with self.assertRaises(ValidationError):
            fiber.update("bad")
        self.assertEqual(applied, ["good"])
        self.assertEqual(fiber.config(), "good")
