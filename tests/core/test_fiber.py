"""Gate-controlled ports of the reference fiber lifecycle and update regressions."""

import asyncio
import unittest
from collections.abc import Awaitable, Callable

import async_timeout

from pykardia import (
    ConfigCommitted,
    Context,
    Disposer,
    Fiber,
    FiberState,
    JsonValue,
    PluginDef,
    StatusChanged,
    ValidationError,
)
from pykardia.core.error import AccessError


async def until(predicate: Callable[[], bool]) -> None:
    """Yield to real runtime tasks until an observable predicate holds, with a finite timeout."""
    async with async_timeout.timeout(2):
        while not predicate():
            await asyncio.sleep(0)


async def noop(context: Context, config: JsonValue) -> None:
    """Provide a real no-op apply body for lifecycle-only scenarios."""


def recording(applied: list[JsonValue]) -> PluginDef:
    """Build a definition recording exactly the configurations passed to apply."""

    async def apply(context: Context, config: JsonValue) -> None:
        """Record each actual episode invocation."""
        applied.append(config)

    return PluginDef.new(apply)


class FiberTests(unittest.IsolatedAsyncioTestCase):
    """Check observable state and cleanup without duplicating lifecycle decisions."""

    async def asyncSetUp(self) -> None:
        """Create a root and bound each asynchronous scenario to a finite duration."""
        self.root = Context()
        self.timeout = async_timeout.timeout(5)
        await self.timeout.__aenter__()

    async def asyncTearDown(self) -> None:
        """Dispose all resources after the test's controlled gates have been released."""
        if not self.timeout.expired:
            await self.root.fiber().dispose()
        await self.timeout.__aexit__(None, None, None)

    async def test_update_config_restart_and_dropped_observer(self) -> None:
        """Port update_config, restart and update_does_not_require_awaiting_its_result."""
        applied: list[JsonValue] = []
        fiber = self.root.plugin_def(recording(applied), "hello")
        self.assertEqual(fiber.state(), FiberState.Loading)
        await fiber.settled()
        await fiber.update("world")
        await fiber.restart()
        fiber.update("last")
        await fiber.settled()
        self.assertEqual(applied, ["hello", "world", "world", "last"])
        self.assertEqual(fiber.config(), "last")

    async def test_failure_refresh_restart_and_update_recovery(self) -> None:
        """A failure resists refresh/restart; update clears it and awaits asynchronous recovery."""
        calls: list[JsonValue] = []
        gate = asyncio.Event()

        async def apply(context: Context, config: JsonValue) -> None:
            """Fail initial loads and hold successful recovery behind a real gate."""
            calls.append(config)
            if config != "fixed":
                raise ValueError(str(config))
            await gate.wait()

        provision = self.root.provide_as("foo", 1)
        fiber = self.root.plugin_def(PluginDef.new(apply).inject(["foo"]), "initial failure")
        with self.assertRaisesRegex(ValueError, "initial failure"):
            await fiber.settled()
        with self.assertRaisesRegex(ValueError, "initial failure"):
            await fiber.restart()
        await provision.dispose()
        self.root.provide_as("foo", 2)
        self.assertEqual(calls, ["initial failure"])
        with self.assertRaisesRegex(ValueError, "retry failure"):
            await fiber.update("retry failure")
        update = asyncio.ensure_future(fiber.update("fixed"))
        await until(lambda: calls[-1] == "fixed")
        self.assertFalse(update.done())
        gate.set()
        await update
        self.assertEqual(fiber.state(), FiberState.Active)

    async def test_fail_from_outside_until_update(self) -> None:
        """Failing from outside withdraws provisions, resists restart and keeps the first error."""
        applied: list[JsonValue] = []

        async def provide(context: Context, config: JsonValue) -> None:
            """Provide a service the consumer waits for."""
            applied.append(config)
            context.provide_as("foo", 1)

        provider = self.root.plugin_def(PluginDef.new(provide).provides(["foo"]))
        consumer = self.root.plugin_def(PluginDef.new(noop).inject(["foo"]))
        await provider.settled()
        await consumer.settled()
        self.assertEqual(consumer.state(), FiberState.Active)
        with self.assertRaisesRegex(ValueError, "^gone$"):
            await provider.fail(ValueError("gone"))
        with self.assertRaisesRegex(ValueError, "^gone$"):
            await provider.fail(ValueError("second"))
        self.assertEqual(provider.state(), FiberState.Failed)
        await until(lambda: consumer.state() is FiberState.Pending)
        with self.assertRaisesRegex(ValueError, "^gone$"):
            await provider.restart()
        await provider.update(None)
        await until(lambda: consumer.state() is FiberState.Active)
        self.assertEqual(len(applied), 2)

    async def test_fail_during_a_load_unloads_it_when_it_finishes(self) -> None:
        """A load in flight completes, then unloads into Failed."""
        gate = asyncio.Event()

        async def apply(context: Context, config: JsonValue) -> None:
            """Provide, then wait behind a real gate."""
            context.provide_as("foo", 1)
            await gate.wait()

        fiber = self.root.plugin_def(PluginDef.new(apply))
        failed = asyncio.ensure_future(fiber.fail(ValueError("gone")))
        await asyncio.sleep(0)
        self.assertEqual(fiber.state(), FiberState.Loading)
        gate.set()
        with self.assertRaisesRegex(ValueError, "^gone$"):
            await failed
        self.assertEqual(fiber.state(), FiberState.Failed)
        with self.assertRaises(AccessError):
            self.root.get_as("foo", int)

    async def test_disposal_state_sequences(self) -> None:
        """Pending, Failed and Active disposal each preserve their reference transitions."""

        async def fail(context: Context, config: JsonValue) -> None:
            """Enter Failed after the runtime completes rollback."""
            raise ValueError("boom")

        pending = self.root.plugin_def(PluginDef.new(noop).inject(["missing"]))
        failed = self.root.plugin_def(PluginDef.new(fail))
        active = self.root.plugin_def(PluginDef.new(noop))
        with self.assertRaises(ValueError):
            await failed.settled()
        await active.settled()
        for fiber, expected in [
            (pending, [(FiberState.Pending, FiberState.Disposed)]),
            (failed, [(FiberState.Failed, FiberState.Disposed)]),
            (
                active,
                [
                    (FiberState.Active, FiberState.Unloading),
                    (FiberState.Unloading, FiberState.Disposed),
                ],
            ),
        ]:
            transitions: list[tuple[FiberState, FiberState]] = []

            def record(
                event: StatusChanged,
                target: Fiber = fiber,
                changes: list[tuple[FiberState, FiberState]] = transitions,
            ) -> None:
                """Record transitions of only the current observed fiber."""
                if event.fiber == target:
                    changes.append((event.old, event.new))

            listener = self.root.on(StatusChanged, record)
            await fiber.dispose()
            self.assertEqual(transitions, expected)
            self.assertTrue(fiber.is_removed())
            await listener.dispose()

    async def test_rejected_update_preserves_instance_and_source(self) -> None:
        """Initial validation fails the fiber; rejected loaded updates leave it untouched."""
        applied: list[JsonValue] = []

        def validate(value: JsonValue) -> JsonValue:
            """Require a true flag and transform the decoded input only."""
            if not isinstance(value, dict) or value.get("ok") is not True:
                raise ValidationError.message("not ok")
            return {**value, "resolved": True}

        definition = recording(applied).validate_with(validate)
        failed = self.root.plugin_def(definition, {"ok": False})
        with self.assertRaises(ValidationError):
            await failed.settled()
        self.assertEqual(applied, [])
        fiber = self.root.plugin_def(definition, {"ok": True})
        await fiber.settled()
        with self.assertRaises(ValidationError):
            fiber.update({"ok": False})
        self.assertEqual(fiber.state(), FiberState.Active)
        self.assertEqual(fiber.config(), {"ok": True})
        self.assertEqual(applied, [{"ok": True, "resolved": True}])

    async def test_decoded_update_hooks_and_commit_timing(self) -> None:
        """Hooks see resolved data, source stays raw, and ConfigCommitted precedes assignment."""
        applied: list[JsonValue] = []
        received: list[dict[str, JsonValue]] = []
        committed: list[tuple[JsonValue, JsonValue]] = []

        def accept(config: dict[str, JsonValue]) -> bool:
            """Take over updates while recording their decoded configuration."""
            received.append(config)
            return config.get("take", True) is True

        async def apply(context: Context, config: JsonValue) -> None:
            """Register a type-specific update hook on the live episode."""
            applied.append(config)
            context.on_update(dict, accept)

        def validate(value: JsonValue) -> JsonValue:
            """Add a decoded-only field."""
            assert isinstance(value, dict)
            return {**value, "resolved": True}

        fiber = self.root.plugin_def(PluginDef.new(apply).validate_with(validate), {"value": 1})
        await fiber.settled()
        self.root.on(
            ConfigCommitted, lambda event: committed.append((event.fiber.config(), event.config))
        )
        await fiber.update({"value": 2})
        await fiber.update_with({"value": 3}, True)
        self.assertEqual(committed, [({"value": 1}, {"value": 2})])
        self.assertEqual(received, [{"value": 2, "resolved": True}, {"value": 3, "resolved": True}])
        self.assertEqual(len(applied), 1)
        self.assertEqual(fiber.config(), {"value": 3})
        await fiber.restart()
        self.assertEqual(applied[-1], {"value": 3, "resolved": True})
        await fiber.update({"value": 4, "take": False})
        self.assertEqual(received[-1], {"value": 4, "take": False, "resolved": True})
        self.assertEqual(applied[-1], received[-1])
        self.assertEqual(fiber.config(), {"value": 4, "take": False})

    async def test_skips_superseded_loads_and_pending_uses_latest(self) -> None:
        """Updates before apply coalesce; pending fibers resolve only the newest source."""
        for inject in ([], ["foo"]):
            applied: list[JsonValue] = []
            fiber = self.root.plugin_def(recording(applied).inject(inject), 1)
            first, second = fiber.update(2), fiber.update(3)
            await asyncio.gather(first, second, fiber.settled())
            if inject:
                self.assertEqual(applied, [])
                self.assertEqual(fiber.state(), FiberState.Pending)
                self.root.provide_as("foo", "available")
                await fiber.settled()
            self.assertEqual(applied, [3])

    async def test_coalesces_updates_during_unloading(self) -> None:
        """A cleanup gate prevents intermediate configurations from starting."""
        unloading, release = asyncio.Event(), asyncio.Event()
        applied: list[JsonValue] = []

        async def apply(context: Context, config: JsonValue) -> None:
            """Hold the first episode's cleanup until later updates are queued."""
            applied.append(config)

            async def dispose() -> None:
                """Pause only the initial inverse."""
                if config == 0:
                    unloading.set()
                    await release.wait()

            context.defer("dispose", Disposer.future(dispose))

        fiber = self.root.plugin_def(PluginDef.new(apply), 0)
        await fiber.settled()
        first = fiber.update(1)
        await unloading.wait()
        second = fiber.update(2)
        self.assertEqual(fiber.state(), FiberState.Unloading)
        self.assertEqual(applied, [0])
        release.set()
        await asyncio.gather(first, second)
        self.assertEqual(applied, [0, 2])

    async def test_concurrent_updates_settle_on_latest(self) -> None:
        """All waiting observers stay pending through a superseded load and newest load."""
        for inject in (False, True):
            await self._concurrent_updates(inject)

    async def _concurrent_updates(self, inject: bool) -> None:
        """Exercise concurrent updates with or without an injected dependency."""
        with self.subTest(inject=inject):
            scope = self.root.isolate("foo")
            scope.provide_as("foo", 1)
            started, release = asyncio.Event(), asyncio.Event()
            latest_started, latest_release = asyncio.Event(), asyncio.Event()
            applied: list[JsonValue] = []
            disposed: list[JsonValue] = []

            async def apply(context: Context, config: JsonValue) -> None:
                """Gate two real episodes and record the inverses they acquire."""
                applied.append(config)
                if inject:
                    self.assertEqual(context.get_as("foo", int).arc(), 1)
                if config == 1:
                    started.set()
                    await release.wait()
                elif config == 3:
                    latest_started.set()
                    await latest_release.wait()
                context.on_dispose(lambda: disposed.append(config))

            definition = PluginDef.new(apply).inject(["foo"] if inject else [])
            fiber = scope.plugin_def(definition, 0)
            await fiber.settled()
            first = asyncio.ensure_future(fiber.update(1))
            await started.wait()
            second, third = (
                asyncio.ensure_future(fiber.update(2)),
                asyncio.ensure_future(fiber.update(3)),
            )
            release.set()
            await latest_started.wait()
            self.assertEqual(applied, [0, 1, 3])
            self.assertEqual(disposed, [0, 1])
            self.assertFalse(any(task.done() for task in (first, second, third)))
            latest_release.set()
            await asyncio.gather(first, second, third)
            await fiber.dispose()
            self.assertEqual(disposed, [0, 1, 3])

    async def test_restart_and_config_aba_during_loading(self) -> None:
        """Equal source values still represent a new revision after restart or an ABA update."""
        for restart in (False, True):
            await self._honors_while_loading(restart)

    async def _honors_while_loading(self, restart: bool) -> None:
        """Hold setup while a restart or ABA config update supersedes it."""
        started, release = asyncio.Event(), asyncio.Event()
        applied: list[JsonValue] = []

        async def apply(context: Context, config: JsonValue) -> None:
            """Hold the original apply after recording its configuration."""
            applied.append(config)
            started.set()
            await release.wait()

        fiber = self.root.plugin_def(PluginDef.new(apply), 1)
        await started.wait()
        updates = [fiber.restart()] if restart else [fiber.update(2), fiber.update(1)]
        release.set()
        await asyncio.gather(*updates)
        self.assertEqual(applied, [1, 1])
        await fiber.dispose()

    async def test_stale_failure_checkpoint_and_retired_load(self) -> None:
        """Superseded failures cannot poison replacements; late inverses are always collected."""
        for outcome in ("failure", "checkpoint", "dispose"):
            await self._stale_load(outcome)

    async def _stale_load(self, outcome: str) -> None:
        """Finish superseded setup under a selected failure or retirement path."""
        started, release = asyncio.Event(), asyncio.Event()
        applied: list[JsonValue] = []
        disposed: list[JsonValue] = []

        async def apply(context: Context, config: JsonValue) -> None:
            """Finish stale setup after its target changes, acquiring one last inverse."""
            applied.append(config)
            if config == 1:
                started.set()
                await release.wait()
            context.on_dispose(lambda: disposed.append(config))
            if config == 1:
                if outcome == "failure":
                    raise ValueError("superseded load")
                if outcome == "checkpoint":
                    context.checkpoint()
                    self.fail("outdated continuation")

        fiber = self.root.plugin_def(PluginDef.new(apply), 1)
        await started.wait()
        update = fiber.update(2)
        disposal: Awaitable[None] | None = fiber.dispose() if outcome == "dispose" else None
        release.set()
        await update
        if disposal is not None:
            await disposal
            self.assertEqual(applied, [1])
            self.assertEqual(fiber.state(), FiberState.Disposed)
        else:
            self.assertEqual(applied, [1, 2])
            self.assertEqual(fiber.state(), FiberState.Active)
        self.assertEqual(disposed, [1])
        await fiber.dispose()

    async def test_latest_failure_rejects_all_waiters(self) -> None:
        """Every waiting update observes the latest episode's failure after full rollback."""
        started, release = asyncio.Event(), asyncio.Event()
        disposed: list[JsonValue] = []

        async def apply(context: Context, config: JsonValue) -> None:
            """Hold the middle episode and fail the final one."""
            context.on_dispose(lambda: disposed.append(config))
            if config == 1:
                started.set()
                await release.wait()
            if config == 2:
                raise ValueError("latest load failed")

        fiber = self.root.plugin_def(PluginDef.new(apply), 0)
        await fiber.settled()
        first = fiber.update(1)
        await started.wait()
        second = fiber.update(2)
        release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
        self.assertEqual([str(error) for error in results], ["latest load failed"] * 2)
        self.assertEqual(disposed, [0, 1, 2])
        self.assertEqual(fiber.state(), FiberState.Failed)

    async def test_hook_accepts_update_during_loading(self) -> None:
        """An accepting hook changes source without superseding its current load."""
        started, release = asyncio.Event(), asyncio.Event()
        applied: list[JsonValue] = []
        received: list[int] = []

        def accept(config: int) -> bool:
            """Accept the source update without triggering another episode."""
            received.append(config)
            return True

        async def apply(context: Context, config: JsonValue) -> None:
            """Register the hook before waiting on asynchronous initialization."""
            applied.append(config)
            context.on_update(int, accept)
            started.set()
            await release.wait()

        fiber = self.root.plugin_def(PluginDef.new(apply), 1)
        await started.wait()
        await fiber.update(2)
        release.set()
        await fiber.settled()
        self.assertEqual((applied, received, fiber.config()), ([1], [2], 2))

    async def test_inertia_locks(self) -> None:
        """Port the first two inertia cases using setup and cleanup gates."""
        for replace_while_loading in (False, True):
            await self._inertia_lock(replace_while_loading)

    async def _inertia_lock(self, replace_while_loading: bool) -> None:
        """Control replacement before or after old setup enters its drain."""
        scope = self.root.isolate("foo")
        provision = scope.provide_as("foo", 1)
        started, release, draining, finish = (asyncio.Event() for _ in range(4))
        values: list[int] = []

        async def apply(context: Context) -> None:
            """Hold setup, then keep the old binding readable throughout cleanup."""
            values.append(context.get_as("foo", int).arc())
            started.set()
            await release.wait()

            async def cleanup() -> None:
                """Expose the unloading interval and retain the committed service view."""
                draining.set()
                await finish.wait()

            context.defer("cleanup", Disposer.future(cleanup))

        fiber = scope.inject(["foo"], apply)
        await started.wait()
        withdrawal = provision.dispose()
        self.assertEqual(fiber.state(), FiberState.Loading)
        if replace_while_loading:
            scope.provide_as("foo", 2)
        release.set()
        await draining.wait()
        self.assertEqual(fiber.state(), FiberState.Unloading)
        if not replace_while_loading:
            scope.provide_as("foo", 2)
        finish.set()
        await withdrawal
        await fiber.settled()
        self.assertEqual(values, [1, 2])
        self.assertEqual(fiber.state(), FiberState.Active)
        await fiber.dispose()

    async def test_warns_about_declared_dependency_cycles(self) -> None:
        """Cycles warn in registration order while both participants remain Pending."""
        first = self.root.plugin_def(PluginDef.new(noop).name("a").inject(["y"]).provides(["x"]))
        second = self.root.plugin_def(PluginDef.new(noop).name("b").inject(["x"]).provides(["y"]))
        self.assertEqual(
            [message.text for message in self.root.log_buffer()], ["dependency cycle: b -> a -> b"]
        )
        self.assertEqual((first.state(), second.state()), (FiberState.Pending, FiberState.Pending))

    async def test_consumer_config_update_during_provider_reload(self) -> None:
        """Port update_config_while_injected_service_reloads with actual dependent episodes."""
        applied: list[tuple[int, JsonValue]] = []

        async def provide(context: Context, config: JsonValue) -> None:
            """Bind the provider revision's value."""
            context.provide_as("provider", config)

        async def consume(context: Context, config: JsonValue) -> None:
            """Observe both the committed provider value and newest source config."""
            applied.append((context.get_as("provider", int).arc(), config))

        provider = self.root.plugin_def(PluginDef.new(provide), 1)
        consumer = self.root.plugin_def(PluginDef.new(consume).inject(["provider"]), "old")
        await provider.settled()
        await consumer.settled()
        await asyncio.gather(provider.update(2), consumer.update("new"))
        await consumer.settled()
        self.assertEqual(applied, [(1, "old"), (2, "new")])

    async def test_failure_rolls_back_listeners_and_logs_aggregate_members(self) -> None:
        """Failed apply cleans its registered listeners and logs errors with member identity."""
        from pykardia import AggregateError, Event

        class Ping(Event[None]):
            """An actual event used to detect leaked failed-episode listeners."""

        calls: list[JsonValue] = []

        async def apply(context: Context, config: JsonValue) -> None:
            """Acquire a listener before an optional aggregate apply failure."""
            context.on(Ping, lambda event: calls.append(config))
            if config == "fail":
                raise AggregateError("outer", [ValueError("first"), ValueError("second")])

        definition = PluginDef.new(apply)
        failed = self.root.plugin_def(definition, "fail")
        active = self.root.plugin_def(definition, "active")
        with self.assertRaises(AggregateError):
            await failed.settled()
        await active.settled()
        self.root.emit(Ping())
        self.assertEqual(calls, ["active"])
        self.assertEqual([message.text for message in self.root.log_buffer()], ["first", "second"])

    async def test_dropped_update_failure_still_rolls_back(self) -> None:
        """Dropping an update observer neither cancels apply nor suppresses its failed state."""
        calls: list[JsonValue] = []
        cleaned: list[JsonValue] = []

        async def apply(context: Context, config: JsonValue) -> None:
            """Acquire a real inverse before the update's deliberate apply failure."""
            calls.append(config)
            context.on_dispose(lambda: cleaned.append(config))
            if config == 1:
                raise ValueError("boom")

        fiber = self.root.plugin_def(PluginDef.new(apply), 0)
        await fiber.settled()
        fiber.update(1)
        with self.assertRaisesRegex(ValueError, "boom"):
            await fiber.settled()
        self.assertEqual(calls, [0, 1])
        self.assertEqual(cleaned, [0, 1])
