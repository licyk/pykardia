"""Source index.rs lifecycle and concurrent loader/HMR acceptance."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import async_timeout

from pykardia import Context, Disposer, Fiber, FiberState, JsonValue, Level, PluginDef
from pykardia.hmr import Exports, Imports, ModuleCode, ReconcileResult
from pykardia.loader import EntryHandle, GroupHandle, MemoryTree
from tests.hmr.common import APP, HmrCase, Query, value


@dataclass
class TreeStats:
    """Observe actual nested plugin episodes and optional host application delay."""

    stall: float = 0
    applied: int = 0
    disposed: int = 0


@dataclass
class Gate:
    """Hold real unload inverses while source operations race with replacement."""

    acquired: int = 0
    applies: int = 0
    fibers: list[Fiber] = field(default_factory=list)
    shared: PluginDef | None = None
    barrier: tuple[asyncio.Event, asyncio.Event] | None = None

    def arm(self) -> tuple[asyncio.Event, asyncio.Event]:
        """Pause subsequent real cleanup callbacks until the release event is set."""
        self.barrier = (asyncio.Event(), asyncio.Event())
        return self.barrier

    def live(self) -> list[Fiber]:
        """Report only retained fibers that have not been retired."""
        return [fiber for fiber in self.fibers if not fiber.is_removed()]


def gated(gate: Gate, version: str) -> ModuleCode:
    """Build the source fixture that counts acquisition and gates actual unload."""

    async def apply(ctx: Context, config: JsonValue) -> None:
        """Acquire the fixture resource and register its actual deferred cleanup."""
        fiber = ctx.fiber()
        gate.shared = fiber.def_()
        gate.acquired += 1
        gate.applies += 1
        gate.fibers.append(fiber)
        ctx.on(Query, lambda query: version if query.name == "value" else None)

        async def unload() -> None:
            """Release observable ownership before waiting at the optional unload barrier."""
            gate.acquired -= 1
            gate.fibers.remove(fiber)
            if gate.barrier is not None:
                entered, release = gate.barrier
                entered.set()
                await release.wait()

        ctx.defer("gate", Disposer.future(unload))

    return ModuleCode.new([], lambda _: Exports.plugin(PluginDef.new(apply)))


def host(gate: Gate) -> ModuleCode:
    """Instantiate the shared definition without recording a module dependency edge."""

    async def apply(ctx: Context, config: JsonValue) -> None:
        """Mount the exact plugin identity retained by the running fixture."""
        assert gate.shared is not None
        ctx.plugin_def(gate.shared)

    return ModuleCode.new([], lambda _: Exports.plugin(PluginDef.new(apply)))


class IndexTests(HmrCase):
    """Exercise real cleanup, nested trees and shared runtime replacement under gates."""

    async def eventually(self, check: Callable[[], bool]) -> None:
        """Wait for an observable runtime boundary with a finite deadline."""
        async with async_timeout.timeout(5):
            while not check():
                await asyncio.sleep(0.001)

    def no_failures(self, *results: ReconcileResult) -> None:
        """Check the reported surviving fibers and import outcomes for every transaction."""
        for result in results:
            self.assertEqual(result.import_failures, [])
            self.assertTrue(
                all(fiber.state() is not FiberState.Failed for fiber in result.settled_fibers)
            )

    async def tree_app(self) -> TreeStats:
        """Build a host owning a nested entry two fiber levels below its own root."""
        stats = TreeStats()
        self.source.codes[f"{APP}/tree-dep"] = value("v1")

        def nested(imports: Imports) -> Exports:
            """Capture the dependency version while evaluating the child plugin."""
            version = imports.value(f"{APP}/tree-dep")

            async def apply(ctx: Context, config: JsonValue) -> None:
                """Count one actual child and register its listener and cleanup."""
                stats.applied += 1
                ctx.on(Query, lambda query: version if query.name == "nested" else None)

                def dispose() -> None:
                    """Record the actual end of this child episode."""
                    stats.disposed += 1

                ctx.on_dispose(dispose)

            return Exports.plugin(PluginDef.new(apply))

        def tree(imports: Imports) -> Exports:
            """Capture the dependency version while evaluating the host plugin."""
            version = imports.value(f"{APP}/tree-dep")

            async def apply(ctx: Context, config: JsonValue) -> None:
                """Create a group under a plain child fiber, then load the nested entry."""
                ctx.on(Query, lambda query: version if query.name == "tree" else None)
                groups: list[GroupHandle] = []

                async def plain(child: Context, config: JsonValue) -> None:
                    """Give the tree a non-reloaded direct parent fiber."""
                    groups.append(self.loader.create_tree(child, MemoryTree()).root())

                await ctx.plugin_def(PluginDef.new(plain)).settled()
                group = groups[0]
                ctx.defer("tree", Disposer.new(group.stop))
                await group.update([{"id": "nested", "name": f"{APP}/nested"}])
                if stats.stall:
                    await asyncio.sleep(stats.stall)

            return Exports.plugin(PluginDef.new(apply))

        self.source.codes[f"{APP}/nested"] = ModuleCode.new([f"{APP}/tree-dep"], nested)
        await self.load("tree", ModuleCode.new([f"{APP}/tree-dep"], tree))
        await self.eventually(lambda: self.query("nested") == "v1")
        return stats

    async def test_should_keep_a_single_nested_entry_when_its_host_reloads_too(self) -> None:
        """Reloading both levels leaves one child and excludes retired fibers from the result."""
        stats = await self.tree_app()
        self.source.codes[f"{APP}/tree-dep"] = value("v2")
        result = await self.reconcile(f"{APP}/tree-dep")
        self.no_failures(result)
        self.assertEqual((self.query("tree"), self.query("nested")), ("v2", "v2"))
        self.assertEqual(stats.applied - stats.disposed, 1)
        self.assertTrue(all(not fiber.is_removed() for fiber in result.settled_fibers))

    async def test_should_keep_a_single_nested_entry_across_a_batch_queued_behind_a_stalled_host(
        self,
    ) -> None:
        """The queued batch waits for a stalled host and then replaces its rebuilt subtree."""
        stats = await self.tree_app()
        stats.stall = 0.3
        self.source.codes[f"{APP}/tree-dep"] = value("v2")
        first = asyncio.create_task(self.reconcile(f"{APP}/tree-dep"))
        await self.eventually(lambda: self.query("nested") == "v2")
        stats.stall = 0
        self.source.codes[f"{APP}/tree-dep"] = value("v3")
        second = await self.reconcile(f"{APP}/tree-dep")
        self.no_failures(await first, second)
        self.assertEqual((self.query("tree"), self.query("nested")), ("v3", "v3"))
        self.assertEqual(stats.applied - stats.disposed, 1)

    async def drain(self) -> tuple[list[str], list[bool]]:
        """Create shared-dependent slow and fast plugins with a real exclusive resource."""
        order: list[str] = []
        overlaps: list[bool] = []
        held = False
        self.source.codes[f"{APP}/drain-dep"] = value("v1")

        def slow(imports: Imports) -> Exports:
            """Capture the dependency version for the resource-owning plugin."""
            version = imports.value(f"{APP}/drain-dep")

            async def apply(ctx: Context, config: JsonValue) -> None:
                """Acquire the resource and make its actual release asynchronous."""
                nonlocal held
                if held:
                    overlaps.append(True)
                held = True
                order.append("slow apply")
                ctx.on(Query, lambda query: version if query.name == "slow" else None)

                async def release() -> None:
                    """Hold the resource through a real delayed cleanup."""
                    nonlocal held
                    order.append("slow dispose")
                    await asyncio.sleep(0.2)
                    held = False
                    order.append("slow released")

                ctx.defer("slow", Disposer.future(release))

            return Exports.plugin(PluginDef.new(apply))

        def fast(imports: Imports) -> Exports:
            """Capture the version for an independent plugin in the same transaction."""
            version = imports.value(f"{APP}/drain-dep")

            async def apply(ctx: Context, config: JsonValue) -> None:
                """Record actual application without blocking on the other plugin."""
                order.append("fast apply")
                ctx.on(Query, lambda query: version if query.name == "fast" else None)

            return Exports.plugin(PluginDef.new(apply))

        await self.load("slow", ModuleCode.new([f"{APP}/drain-dep"], slow))
        await self.load("fast", ModuleCode.new([f"{APP}/drain-dep"], fast))
        order.clear()
        return order, overlaps

    async def test_should_not_start_the_new_instance_before_the_old_one_released(self) -> None:
        """Each resource-owning instance starts only after its old cleanup finishes."""
        order, overlaps = await self.drain()
        self.source.codes[f"{APP}/drain-dep"] = value("v2")
        await self.reconcile(f"{APP}/drain-dep")
        self.assertEqual(overlaps, [])
        self.assertLess(order.index("slow released"), order.index("slow apply"))
        self.assertEqual(self.query("slow"), "v2")

    async def test_should_not_delay_an_unrelated_plugin_behind_a_slow_one(self) -> None:
        """Concurrent settling lets the fast plugin apply while the slow plugin is draining."""
        order, _ = await self.drain()
        self.source.codes[f"{APP}/drain-dep"] = value("v3")
        await self.reconcile(f"{APP}/drain-dep")
        self.assertLess(order.index("slow dispose"), order.index("fast apply"))
        self.assertLess(order.index("fast apply"), order.index("slow released"))
        self.assertEqual(self.query("fast"), "v3")

    async def test_should_rebuild_every_fiber_each_keeping_its_own_config(self) -> None:
        """Two entries share one runtime and each applies exactly once with its own config."""
        applied: list[str] = []

        def code(version: str) -> ModuleCode:
            """Build a fresh module whose plugin reports each instance's own label."""

            async def apply(ctx: Context, config: JsonValue) -> None:
                """Record the actual source config delivered to this fiber."""
                assert isinstance(config, dict)
                label = str(config["label"])
                applied.append(f"{label}:{version}")
                ctx.on(Query, lambda query: version if query.name == label else None)

            return ModuleCode.new([], lambda _: Exports.plugin(PluginDef.new(apply)))

        self.source.codes[f"{APP}/multi"] = code("v1")
        for label in ["a", "b"]:
            await self.loader.create(
                {"id": label, "name": f"{APP}/multi", "config": {"label": label}}
            )
        await self.loader.settled()
        fibers = [self.loader.resolve(id).inner_fiber() for id in ["a", "b"]]
        applied.clear()
        self.source.codes[f"{APP}/multi"] = code("v2")
        result = await self.reconcile(f"{APP}/multi")
        self.assertEqual(result.settled_fibers, fibers)
        self.assertCountEqual(applied, ["a:v2", "b:v2"])
        self.assertEqual((self.query("a"), self.query("b")), ("v2", "v2"))

    async def gate_app(self) -> tuple[Gate, EntryHandle]:
        """Start one actual gate plugin before arming its cleanup barrier."""
        gate = Gate()
        await self.load("live", gated(gate, "initial"))
        self.assertEqual(gate.acquired, 1)
        gate.applies = 0
        return gate, self.loader.resolve("live")

    async def test_does_not_rebuild_an_entry_disabled_during_unload(self) -> None:
        """Disabling a held reload retires its instance without applying the replacement."""
        gate, entry = await self.gate_app()
        previous = entry.inner_fiber()
        assert previous is not None
        entered, release = gate.arm()
        self.source.codes[f"{APP}/live"] = gated(gate, "should-not-live")
        reloaded = asyncio.create_task(self.reconcile(f"{APP}/live"))
        async with async_timeout.timeout(5):
            await entered.wait()
            await self.loader.update("live", {"disabled": True})
            release.set()
            await reloaded
        self.assertTrue(entry.is_disabled())
        self.assertIs(entry.inner_fiber(), previous)
        self.assertTrue(previous.is_removed())
        self.assertIsNone(self.query("value"))
        self.assertEqual(gate.applies, 0)
        self.assertEqual(gate.live(), [])

    async def test_does_not_rebuild_an_entry_removed_during_unload(self) -> None:
        """Removal during held cleanup does not turn into a fresh replacement instance."""
        gate, entry = await self.gate_app()
        previous = entry.inner_fiber()
        assert previous is not None
        entered, release = gate.arm()
        self.source.codes[f"{APP}/live"] = gated(gate, "removed-value")
        reloaded = asyncio.create_task(self.reconcile(f"{APP}/live"))
        async with async_timeout.timeout(5):
            await entered.wait()
            self.loader.remove("live")
            self.assertFalse(entry.is_disabled())
            release.set()
            await reloaded
        self.assertIs(entry.inner_fiber(), previous)
        self.assertTrue(previous.is_removed())
        self.assertIsNone(self.query("value"))
        self.assertEqual(gate.applies, 0)
        self.assertEqual(gate.live(), [])

    async def test_waits_for_a_second_re_enable_init_before_deciding_whether_to_rebuild(
        self,
    ) -> None:
        """Two re-enables racing a held reload produce exactly their two new episodes."""
        gate, entry = await self.gate_app()
        previous = entry.inner_fiber()
        assert previous is not None
        entered, release = gate.arm()
        self.source.codes[f"{APP}/live"] = gated(gate, "second-init")
        reloaded = asyncio.create_task(self.reconcile(f"{APP}/live"))
        async with async_timeout.timeout(5):
            await entered.wait()
            holds = [asyncio.Event(), asyncio.Event()]
            patches: list[bool] = []

            async def patch(patched: EntryHandle, next: Awaitable[None]) -> None:
                """Pause each real initialization at the source test's context-patch boundary."""
                if patched.id() == entry.id():
                    patches.append(True)
                    if len(patches) <= 2:
                        await holds[len(patches) - 1].wait()
                await next

            hook = self.loader.on_patch_context(self.root, patch)
            await self.loader.update("live", {"disabled": True})
            updating = asyncio.create_task(self.loader.update("live", {"disabled": False}))
            await self.eventually(lambda: len(patches) == 1)

            async def reenable() -> Fiber | None:
                """Join the first start, then disable and start the same entry a second time."""
                await entry.init()
                installed = entry.inner_fiber()
                if installed is not None:
                    await installed.settled()
                await self.loader.update("live", {"disabled": True})
                await self.loader.update("live", {"disabled": False})
                return installed

            reenabled = asyncio.create_task(reenable())
            release.set()
            await previous.settled()
            holds[0].set()
            await self.eventually(lambda: len(patches) == 2)
            holds[1].set()
            await updating
            installed = await reenabled
            await reloaded
            if installed is not None:
                await installed.settled()
            await self.loader.settled()
            await hook.dispose()
        self.assertEqual(len(patches), 2)
        self.assertEqual(self.query("value"), "second-init")
        self.assertEqual(gate.applies, 2)
        self.assertEqual(gate.live(), [entry.inner_fiber()])

    async def test_rebuilds_a_child_of_an_active_host_that_reused_the_plugin(self) -> None:
        """Replacement reaches a shared runtime child without replacing its unaffected host."""
        gate, entry = await self.gate_app()
        previous = entry.inner_fiber()
        await self.load("host", host(gate))
        host_entry = self.loader.resolve("host")
        host_root = host_entry.inner_fiber()
        assert host_root is not None
        await self.eventually(lambda: len(gate.live()) == 2)
        applies = gate.applies
        entered, release = gate.arm()
        self.source.codes[f"{APP}/live"] = gated(gate, "from-host")
        reloaded = asyncio.create_task(self.reconcile(f"{APP}/live"))
        async with async_timeout.timeout(5):
            await entered.wait()
            await self.eventually(lambda: gate.acquired == 0)
            release.set()
            await reloaded
        self.assertEqual(self.query("value"), "from-host")
        self.assertEqual(gate.applies - applies, 2)
        self.assertIs(entry.inner_fiber(), previous)
        self.assertEqual(len(gate.live()), 2)
        self.assertIs(host_entry.inner_fiber(), host_root)
        await self.loader.update("host", {"disabled": True})
        await host_root.settled()
        self.assertTrue(host_root.is_removed())
        self.assertEqual(gate.live(), [entry.inner_fiber()])

    async def test_skips_a_child_whose_host_is_replaced_during_unload(self) -> None:
        """A stale child is excluded after its host is disabled and re-enabled during cleanup."""
        gate, _ = await self.gate_app()
        await self.load("host", host(gate))
        host_entry = self.loader.resolve("host")
        host_root = host_entry.inner_fiber()
        assert host_root is not None
        await self.eventually(lambda: len(gate.live()) == 2)
        entered, release = gate.arm()
        self.source.codes[f"{APP}/live"] = gated(gate, "host-replaced")
        reloaded = asyncio.create_task(self.reconcile(f"{APP}/live"))
        async with async_timeout.timeout(5):
            await entered.wait()
            await self.eventually(lambda: gate.acquired == 0)
            await self.loader.update("host", {"disabled": True})
            await self.loader.update("host", {"disabled": False})
            self.assertTrue(host_root.is_removed())
            current = host_entry.inner_fiber()
            assert current is not None
            self.assertFalse(current.is_removed())
            release.set()
            result = await reloaded
        self.no_failures(result)
        self.assertEqual(
            [log.text for log in self.root.log_buffer() if log.level is Level.Error], []
        )
        await self.eventually(lambda: len(gate.live()) == 2)
