"""Controlled asynchronous import, patch and cleanup interleavings."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable

from pykardia import Context, Disposer, Event, FiberState, JsonValue, PluginDef
from pykardia.loader import EntryHandle
from tests.loader.common import Counter, Hold, LoaderCase

INJECTIONS: tuple[JsonValue, ...] = (None, ["settings"])


class Ping(Event[None]):
    """Detect whether an obsolete plugin listener was actually cleaned up."""


class RebuildTests(LoaderCase):
    """Gate real runtime boundaries and assert only the latest entry can become active."""

    async def test_rename_active_with_and_without_outer(self) -> None:
        """Name changes retire the old plugin without persisting a disabled flag."""
        self.root.provide_as("settings", {})
        first, second = self.register("a"), self.register("b")
        for index, inject in enumerate(INJECTIONS):
            identity = str(index)
            await self.loader.create({"id": identity, "name": "a", "inject": inject})
            await self.loader.settled()
            old = self.fiber(identity)
            await self.loader.update(identity, {"name": "b"})
            await self.loader.settled()
            self.assertTrue(old.is_removed())
            self.assertEqual(self.fiber(identity).state(), FiberState.Active)
            self.assertEqual(
                self.loader.resolve(identity).options(),
                {"id": identity, "name": "b", "inject": inject},
            )
        self.assertEqual((first.applied, second.applied), (2, 2))
        self.assertTrue(
            all(
                not isinstance(change.options, dict) or "disabled" not in change.options
                for change in self.backend.changes
            )
        )

    async def test_latest_name_during_import_success_or_failure(self) -> None:
        """Concurrent renames join one import and discard stale success or failure."""
        self.register("a")
        second, latest = self.register("b"), self.register("c")
        for index, fail in enumerate((False, True)):
            identity = str(index)
            await self.loader.create({"id": identity, "name": "a"})
            await self.loader.settled()
            hold = Hold("b", fail=fail)
            self.resolver.hold = hold
            first = asyncio.create_task(self.loader.update(identity, {"name": "b"}))
            await hold.entered.wait()
            last = asyncio.create_task(self.loader.update(identity, {"name": "c"}))
            while self.loader.resolve(identity).options() != {"id": identity, "name": "c"}:
                await asyncio.sleep(0)
            self.assertTrue(self.loader.is_busy())
            hold.release.set()
            await asyncio.gather(first, last)
            await self.loader.settled()
            self.assertEqual(self.fiber(identity).state(), FiberState.Active)
        self.assertEqual((second.applied, latest.applied), (0, 2))

    async def test_latest_name_during_context_patch(self) -> None:
        """A rename after the old context was patched still prevents stale application."""
        self.register("a")
        second, latest = self.register("b"), self.register("c")
        await self.read([{"id": "x", "name": "a"}])
        entered, release = asyncio.Event(), asyncio.Event()

        async def hook(entry: EntryHandle, next_: Awaitable[None]) -> None:
            """Hold after the actual context patch only for the intermediate name."""
            selected = entry.options() == {"id": "x", "name": "b"}
            await next_
            if selected:
                entered.set()
                await release.wait()

        self.loader.on_patch_context(self.root, hook)
        first = asyncio.create_task(self.loader.update("x", {"name": "b"}))
        await entered.wait()
        last = asyncio.create_task(self.loader.update("x", {"name": "c"}))
        while self.loader.resolve("x").options() != {"id": "x", "name": "c"}:
            await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, last)
        await self.loader.settled()
        self.assertEqual((second.applied, latest.applied), (0, 1))

    async def test_join_rename_while_cleanup_pending(self) -> None:
        """The second rename joins one cleanup task and applies only the newest definition."""
        entered, release = asyncio.Event(), asyncio.Event()
        second, latest = self.register("b"), self.register("c")

        async def apply(ctx: Context, config: JsonValue) -> None:
            """Install a real asynchronous disposer that blocks the first rebuild."""

            async def cleanup() -> None:
                """Expose the exact transition into old-instance cleanup."""
                entered.set()
                await release.wait()

            ctx.defer("held", Disposer.future(cleanup))

        self.catalog.register_def("a", PluginDef.new(apply))
        await self.read([{"id": "x", "name": "a"}])
        first = asyncio.create_task(self.loader.update("x", {"name": "b"}))
        await entered.wait()
        last = asyncio.create_task(self.loader.update("x", {"name": "c"}))
        while self.loader.resolve("x").options() != {"id": "x", "name": "c"}:
            await asyncio.sleep(0)
        self.assertTrue(self.loader.is_busy())
        self.assertEqual((second.applied, latest.applied), (0, 0))
        release.set()
        await asyncio.gather(first, last)
        await self.loader.settled()
        self.assertEqual((second.applied, latest.applied), (0, 1))

    async def test_remove_during_cleanup_prevents_rebuild(self) -> None:
        """Unregistering while an obsolete episode drains prevents the replacement start."""
        entered, release = asyncio.Event(), asyncio.Event()
        replacement = self.register("b")
        self.root.provide_as("settings", {})

        async def apply(ctx: Context, config: JsonValue) -> None:
            """Collect an asynchronous inverse under a real entry outer fiber."""

            async def cleanup() -> None:
                """Wait until the entry has been unregistered before finishing disposal."""
                entered.set()
                await release.wait()

            ctx.defer("held", Disposer.future(cleanup))

        self.catalog.register_def("a", PluginDef.new(apply))
        await self.read([{"id": "x", "name": "a", "inject": ["settings"]}])
        entry = self.loader.resolve("x")
        pending = asyncio.create_task(self.loader.update("x", {"name": "b"}))
        await entered.wait()
        self.loader.remove("x")
        release.set()
        await pending
        await self.loader.settled()
        self.assertEqual(replacement.applied, 0)
        self.assertIsNone(entry.outer_fiber())
        inner = entry.inner_fiber()
        self.assertTrue(inner is None or inner.is_removed())

    async def test_pending_inject_clear_and_old_listener_cleanup(self) -> None:
        """Adding an unavailable dependency removes listeners; clearing it starts a new episode."""
        hits: list[int] = []

        async def apply(ctx: Context, config: JsonValue) -> None:
            """Register an actual resource-owned event listener."""
            ctx.on(Ping, lambda event: hits.append(1))

        self.catalog.register_def("plug", PluginDef.new(apply))
        await self.read([{"id": "x", "name": "plug"}])
        old = self.fiber("x")
        self.root.emit(Ping())
        await self.loader.update("x", {"inject": ["missing"]})
        await self.loader.settled()
        outer = self.loader.resolve("x").outer_fiber()
        assert outer is not None
        self.assertTrue(old.is_removed())
        self.assertEqual(outer.state(), FiberState.Pending)
        self.root.emit(Ping())
        self.assertEqual(hits, [1])
        await self.loader.update("x", {"inject": None})
        await self.loader.settled()
        self.assertTrue(outer.is_removed())
        self.root.emit(Ping())
        self.assertEqual(hits, [1, 1])

    async def test_remove_or_disable_during_import(self) -> None:
        """A dropped initialization releases its barrier and allows a later create or enable."""
        self.root.provide_as("settings", {})
        counter = self.register("worker")
        for inject in INJECTIONS:
            for action in ("remove", "disable"):
                options: JsonValue = {"id": "x", "name": "worker", "inject": inject}
                hold = Hold("worker")
                self.resolver.hold = hold
                creating = asyncio.create_task(self.loader.create(options))
                await hold.entered.wait()
                entry = self.loader.resolve("x")
                if action == "remove":
                    self.loader.remove("x")
                else:
                    await self.loader.update("x", {"disabled": True})
                hold.release.set()
                await creating
                await self.loader.settled()
                self.assertIsNone(entry.outer_fiber())
                self.assertIsNone(entry.inner_fiber())
                self.assertEqual(counter.applied, 0)
                if action == "remove":
                    await self.loader.create(options)
                else:
                    await self.loader.update("x", {"disabled": False})
                await self.loader.settled()
                self.assertEqual(self.fiber("x").state(), FiberState.Active)
                self.loader.remove("x")
                await self.loader.settled()
                counter.applied = 0

    async def test_remove_or_disable_during_patch(self) -> None:
        """A suspended hook cannot resurrect an entry removed or disabled before continuation."""
        self.root.provide_as("settings", {})
        counter = self.register("worker")
        for inject in INJECTIONS:
            for action in ("remove", "disable"):
                entered, release = asyncio.Event(), asyncio.Event()

                async def hook(
                    entry: EntryHandle,
                    next_: Awaitable[None],
                    entered: asyncio.Event = entered,
                    release: asyncio.Event = release,
                ) -> None:
                    """Hold the real patch continuation until after the source mutation."""
                    entered.set()
                    await release.wait()
                    await next_

                handle = self.loader.on_patch_context(self.root, hook)
                options: JsonValue = {"id": "x", "name": "worker", "inject": inject}
                creating = asyncio.create_task(self.loader.create(options))
                await entered.wait()
                entry = self.loader.resolve("x")
                if action == "remove":
                    self.loader.remove("x")
                else:
                    await self.loader.update("x", {"disabled": True})
                release.set()
                await creating
                await handle.dispose()
                await self.loader.settled()
                self.assertIsNone(entry.outer_fiber())
                self.assertIsNone(entry.inner_fiber())
                self.assertEqual(counter.applied, 0)
                if action == "remove":
                    await self.loader.create(options)
                else:
                    await self.loader.update("x", {"disabled": False})
                await self.loader.settled()
                self.assertEqual(self.fiber("x").state(), FiberState.Active)
                self.loader.remove("x")
                await self.loader.settled()
                counter.applied = 0

    async def test_hooks_order_and_lazy_continuation(self) -> None:
        """Hooks wrap in registration order and an unawaited next creates no work or coroutine."""
        order: list[str] = []
        counter = self.register("worker")

        async def first(entry: EntryHandle, next_: Awaitable[None]) -> None:
            """Observe the outermost continuation before and after the rest of the chain."""
            order.append("first:before")
            await next_
            order.append("first:after")

        async def stop(entry: EntryHandle, next_: Awaitable[None]) -> None:
            """Omit continuation to prove later hooks do not run eagerly."""
            order.append("stop")

        async def last(entry: EntryHandle, next_: Awaitable[None]) -> None:
            """Record whether the skipped continuation was actually invoked."""
            order.append("last")
            await next_

        self.loader.on_patch_context(self.root, first)
        stopped = self.loader.on_patch_context(self.root, stop)
        self.loader.on_patch_context(self.root, last)
        await self.loader.create({"id": "x", "name": "worker"})
        await self.loader.settled()
        self.assertEqual(order, ["first:before", "stop", "first:after"])
        await stopped.dispose()
        order.clear()
        await self.loader.update("x", {"config": 1})
        await self.loader.settled()
        self.assertEqual(order, ["first:before", "last", "first:after"])
        self.assertGreaterEqual(counter.applied, 1)

    async def test_dependency_loss_during_outer_import(self) -> None:
        """An outer activation checkpoints after its dependency disappears during resolution."""
        provision = self.root.provide_as("settings", {})
        counter = Counter()
        self.catalog.register_def("worker", counter.definition())
        hold = Hold("worker", skip=1)
        self.resolver.hold = hold
        await self.loader.create({"id": "x", "name": "worker", "inject": ["settings"]})
        await hold.entered.wait()
        disposal = provision.dispose()
        hold.release.set()
        await disposal
        await self.loader.settled()
        self.assertEqual(counter.applied, 0)
        self.assertIsNone(self.loader.resolve("x").inner_fiber())
