"""Group admission and remaining evaluation/ownership boundaries."""

from __future__ import annotations

import asyncio

from pykardia import Context, Disposer, FiberState, JsonValue, PluginDef
from pykardia.loader import TreeHandle
from tests.loader.common import LoaderCase, Recorded
from tests.loader.test_evaluate import provider, settings, worker


class BoundaryTests(LoaderCase):
    """Check nested structural groups and entry-specific access authorization."""

    async def test_nested_group_disable_and_enable(self) -> None:
        """Clearing an inner disable does not override a still-disabled ancestor group."""
        counter = self.register("foo")
        outer = await self.loader.create(
            {"name": "group", "group": True, "config": [{"name": "foo"}]}
        )
        inner = await self.loader.create(
            {"name": "group", "group": True, "config": [{"name": "foo"}]}, parent=outer
        )
        await self.loader.settled()
        self.assertEqual((counter.applied, counter.disposed), (2, 0))
        for identifier, disabled, delta in (
            (inner, True, (0, 1)),
            (outer, True, (0, 1)),
            (inner, None, (0, 0)),
            (outer, None, (2, 0)),
        ):
            before = counter.applied, counter.disposed
            await self.loader.update(identifier, {"disabled": disabled})
            await self.loader.settled()
            self.assertEqual((counter.applied - before[0], counter.disposed - before[1]), delta)
        self.assertEqual(len(self.loader.entries()), 4)

    async def test_transfer_through_enabled_and_disabled_groups(self) -> None:
        """Moves retain an active episode only while old and new ancestor verdicts permit it."""
        counter = self.register("foo")
        await self.read(
            [
                {"id": "x", "name": "foo"},
                {
                    "id": "alpha",
                    "name": "group",
                    "group": True,
                    "config": [
                        {
                            "id": "beta",
                            "name": "group",
                            "group": True,
                            "disabled": True,
                            "config": [
                                {"id": "gamma", "name": "group", "group": True, "config": []}
                            ],
                        }
                    ],
                },
            ]
        )
        for parent, delta in (
            ("alpha", (0, 0)),
            ("beta", (0, 1)),
            ("gamma", (0, 0)),
            (None, (1, 0)),
        ):
            before = counter.applied, counter.disposed
            await self.loader.update("x", {}, parent=parent)
            await self.loader.settled()
            self.assertEqual((counter.applied - before[0], counter.disposed - before[1]), delta)
        self.assertEqual(len(self.loader.entries()), 4)

    async def test_tree_dependency_loss_does_not_disable_children(self) -> None:
        """Dependency loss unloads tree children without persisting their disposal."""
        self.register("bar")
        backend = Recorded()
        trees: list[TreeHandle] = []

        async def apply(ctx: Context, config: JsonValue) -> None:
            """Create a real owned tree whose unregister runs after child disposal."""
            tree = self.loader.create_tree(ctx, backend)
            trees.append(tree)

            async def cleanup() -> None:
                """Delay unregistering to exercise parent-unload self-disposal detection."""
                await asyncio.sleep(0)
                tree.root().stop()

            ctx.defer("tree", Disposer.future(cleanup))
            await tree.root().update([{"id": "a", "name": "bar"}])

        self.catalog.register_def("tree", PluginDef.new(apply).inject(["foo"]))
        provision = self.root.provide_as("foo", None)
        await self.read([{"id": "tree", "name": "tree"}])
        self.assertEqual(self.fiber("tree:a").state(), FiberState.Active)
        await provision.dispose()
        await self.loader.settled()
        self.assertEqual(self.fiber("tree").state(), FiberState.Pending)
        self.assertEqual(trees[0].root().data(), [{"id": "a", "name": "bar"}])
        self.assertEqual(backend.changes, [])

    async def test_config_interpolation_waits_for_outer_and_reloads(self) -> None:
        """Source config expressions wait for declared entry dependencies and rerun on recovery."""
        counter = self.register("worker")
        self.catalog.register_def("settings", provider("settings"))
        expression: JsonValue = {"value": {"__jsExpr": "settings.value"}}
        await self.read([worker({"inject": ["settings"], "config": expression})])
        self.assertIsNone(self.loader.resolve("worker").inner_fiber())
        await self.loader.create(settings({"value": 1}))
        await self.loader.settled()
        await self.loader.update("settings", {"config": {"value": 2}})
        await self.loader.settled()
        self.assertEqual(counter.configs, [{"value": 1}, {"value": 2}])
        self.assertEqual(self.fiber("worker").config(), expression)

    async def test_intercept_and_disabled_cannot_read_undeclared_siblings(self) -> None:
        """Only parent and entry injection authorize disabled and interception reads."""
        counter = self.register("worker")
        self.catalog.register_def("settings", provider("settings"))
        self.catalog.register_def("env", provider("env"))
        await self.read(
            [
                settings({"enabled": True}),
                {"id": "env", "name": "env"},
                worker({"inject": ["env"], "disabled": {"__jsExpr": "!settings.enabled"}}),
            ]
        )
        entry = self.loader.resolve("worker")
        outer = entry.outer_fiber()
        assert outer is not None
        self.assertEqual(outer.state(), FiberState.Active)
        self.assertTrue(entry.is_disabled())
        self.assertEqual(counter.applied, 0)
        await self.loader.update(
            "worker",
            {
                "inject": ["settings"],
                "disabled": None,
                "intercept": {"http": {"__jsExpr": "settings.missing.value"}},
            },
        )
        await self.loader.settled()
        self.assertTrue(entry.is_disabled())
        self.assertEqual(counter.applied, 0)
