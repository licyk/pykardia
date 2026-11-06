"""Remaining loader source/config ownership and nested update regressions."""

from __future__ import annotations

import asyncio

from pykardia import Context, Disposer, FiberState, JsonValue, PluginDef
from tests.loader.common import Counter, Hold, LoaderCase
from tests.loader.test_evaluate import provider, settings, worker


class LoaderRegressionTests(LoaderCase):
    """Verify runtime ownership details through real plugin-created fibers and hooks."""

    async def test_child_config_and_disposal_are_not_entry_mutations(self) -> None:
        """Plugin-created children keep raw config and do not disable their parent on disposal."""
        child = Counter()
        definition = child.definition()
        expression: JsonValue = {"value": {"__jsExpr": "missing.value"}}

        async def parent(ctx: Context, config: JsonValue) -> None:
            """Create and retire an ordinary child while preserving the parent entry."""
            nested = ctx.plugin_def(definition, expression)
            await nested.settled()
            await nested.dispose()

        self.catalog.register_def("parent", PluginDef.new(parent))
        await self.read([{"id": "p", "name": "parent"}])
        self.assertEqual(child.configs, [expression])
        self.assertEqual(self.loader.resolve("p").options(), {"id": "p", "name": "parent"})
        self.assertEqual(self.backend.changes, [])
        self.assertEqual(self.fiber("p").state(), FiberState.Active)

    async def test_pending_fiber_update_persists(self) -> None:
        """A pending inner plugin still commits its explicit config update to the source."""
        counter = Counter()
        self.catalog.register_def("pending", counter.definition().inject(["never"]))
        await self.read([{"id": "p", "name": "pending"}])
        self.assertEqual(self.fiber("p").state(), FiberState.Pending)
        await self.fiber("p").update({"a": 3})
        self.assertEqual(
            self.loader.root().data(), [{"id": "p", "name": "pending", "config": {"a": 3}}]
        )
        self.assertEqual(counter.applied, 0)

    async def test_replace_preserves_entry_ownership(self) -> None:
        """In-place registry replacement preserves the loader entry and later write-back routing."""
        old, new = Counter(), Counter()
        first, second = old.definition(), new.definition()
        self.catalog.register_def("worker", first)
        await self.read([worker({})])
        entry, fiber = self.loader.resolve("worker"), self.fiber("worker")
        self.root.registry().replace(first, second)
        await fiber.settled()
        self.assertEqual(self.loader.entry_of(fiber.context()), entry)
        self.assertEqual(self.fiber("worker"), fiber)
        await fiber.update({"value": 2})
        self.assertEqual(
            entry.options(), {"id": "worker", "name": "worker", "config": {"value": 2}}
        )
        self.assertEqual((old.applied, old.disposed, new.applied), (1, 1, 2))

    async def test_group_update_waits_for_child_resolution(self) -> None:
        """An accepted group update waits for its child's actual resolver before completing."""
        self.register("worker")
        await self.read([{"id": "g", "name": "group", "group": True, "config": []}])
        hold = Hold("worker")
        self.resolver.hold = hold
        update = asyncio.create_task(self.loader.update("g", {"config": [worker({})]}))
        await hold.entered.wait()
        self.assertFalse(update.done())
        hold.release.set()
        await update
        self.assertIsNotNone(self.loader.resolve("worker").inner_fiber())
        await self.loader.settled()

    async def test_nested_intercepts_and_adoption_preserve_source_reads(self) -> None:
        """Nested metadata stays outer-first and source adoption produces no backend writes."""
        counter = self.register("worker")
        await self.read(
            [
                worker({"intercept": {"x": {"c": 3}}}),
                {
                    "id": "g",
                    "name": "group",
                    "group": True,
                    "intercept": {"x": {"a": 1}},
                    "config": [
                        {
                            "id": "inner",
                            "name": "group",
                            "group": True,
                            "intercept": {"x": {"b": 2}},
                            "config": [],
                        }
                    ],
                },
            ]
        )
        await self.read(
            [
                {
                    "id": "g",
                    "name": "group",
                    "group": True,
                    "intercept": {"x": {"a": 1}},
                    "config": [
                        {
                            "id": "inner",
                            "name": "group",
                            "group": True,
                            "intercept": {"x": {"b": 2}},
                            "config": [worker({"intercept": {"x": {"c": 3}}})],
                        }
                    ],
                }
            ]
        )
        self.assertEqual(
            self.fiber("worker").context().intercepts("x"), [{"a": 1}, {"b": 2}, {"c": 3}]
        )
        self.assertEqual(
            self.loader.resolve("worker").parent(), self.loader.resolve("inner").subgroup()
        )
        self.assertEqual(self.backend.changes, [])
        self.assertGreaterEqual(counter.applied, 1)
        await self.read([])
        self.assertEqual(self.backend.changes, [])

    async def test_failed_entry_recovers_from_config_update(self) -> None:
        """A failed inner episode re-evaluates its config under the surviving entry injection."""
        self.catalog.register_def("settings", provider("settings"))
        configs: list[JsonValue] = []
        failing = True

        async def apply(ctx: Context, config: JsonValue) -> None:
            """Fail the first real application and accept the corrected subsequent config."""
            configs.append(config)
            if failing:
                raise ValueError("boom")

        self.catalog.register_def("worker", PluginDef.new(apply))
        await self.read(
            [
                settings({"value": 1}),
                worker(
                    {"inject": ["settings"], "config": {"value": {"__jsExpr": "settings.value"}}}
                ),
            ]
        )
        self.assertEqual(self.fiber("worker").state(), FiberState.Failed)
        failing = False
        await self.loader.update(
            "worker", {"config": {"value": {"__jsExpr": "settings.value"}, "fixed": True}}
        )
        await self.loader.settled()
        self.assertEqual(self.fiber("worker").state(), FiberState.Active)
        self.assertEqual(configs[-1], {"value": 1, "fixed": True})

    async def test_group_intercept_failure_stops_children(self) -> None:
        """An interception failure prevents a structural group itself from running."""
        self.register("worker")
        self.catalog.register_def("settings", provider("settings"))
        await self.read(
            [
                settings({}),
                {
                    "id": "g",
                    "name": "group",
                    "group": True,
                    "inject": ["settings"],
                    "config": [worker({})],
                },
            ]
        )
        child = self.fiber("worker")
        await self.loader.update(
            "g", {"intercept": {"http": {"__jsExpr": "settings.missing.value"}}}
        )
        await self.loader.settled()
        self.assertTrue(self.fiber("g").is_removed())
        self.assertTrue(child.is_removed())
        await self.loader.update("g", {"intercept": None})
        await self.loader.settled()
        self.assertEqual(self.fiber("worker").state(), FiberState.Active)

    async def test_plugin_renames_own_entry_during_apply(self) -> None:
        """An apply-triggered rename stays busy until the original disposer finishes."""
        entered, release = asyncio.Event(), asyncio.Event()
        replacement = self.register("replacement")
        updates: list[asyncio.Task[None]] = []

        async def apply(ctx: Context, config: JsonValue) -> None:
            """Request a real loader rename and install the old episode's blocking inverse."""

            async def cleanup() -> None:
                """Keep the replacement waiting for a real old-instance disposal."""
                entered.set()
                await release.wait()

            ctx.defer("held", Disposer.future(cleanup))
            updates.append(asyncio.create_task(self.loader.update("x", {"name": "replacement"})))

        self.catalog.register_def("renaming", PluginDef.new(apply))
        await self.loader.create({"id": "x", "name": "renaming"})
        await entered.wait()
        self.assertTrue(self.loader.is_busy())
        self.assertEqual(replacement.applied, 0)
        release.set()
        await asyncio.gather(*updates)
        await self.loader.settled()
        self.assertEqual(replacement.applied, 1)
        self.assertEqual(self.fiber("x").state(), FiberState.Active)

    async def test_nested_rebuild_during_apply_joins_latest_inject_update(self) -> None:
        """An apply-triggered second rebuild survives until its own disposer has settled."""
        for key in ("foo", "bar", "qux"):
            self.root.provide_as(key, 1)
        entered, release = asyncio.Event(), asyncio.Event()
        updates: list[asyncio.Task[None]] = []
        applies = 0

        async def apply(ctx: Context, config: JsonValue) -> None:
            """Start another inject update only during the second application episode."""
            nonlocal applies
            applies += 1
            episode = applies

            async def cleanup() -> None:
                """Pause the second episode's cleanup while further updates join it."""
                if episode == 2:
                    entered.set()
                    await release.wait()

            ctx.defer("held", Disposer.future(cleanup))
            if episode == 2:
                updates.append(asyncio.create_task(self.loader.update("x", {"inject": ["bar"]})))

        definition = PluginDef.new(apply)
        self.catalog.register_def("plug", definition)
        await self.read([{"id": "x", "name": "plug"}])
        await self.loader.update("x", {"inject": ["foo"]})
        await entered.wait()
        self.assertTrue(self.loader.is_busy())
        waiting = asyncio.create_task(self.loader.settled())
        third = asyncio.create_task(self.loader.update("x", {"inject": ["qux"]}))
        while self.loader.resolve("x").options() != {"id": "x", "name": "plug", "inject": ["qux"]}:
            await asyncio.sleep(0)
        self.assertFalse(waiting.done())
        self.assertEqual(applies, 2)
        release.set()
        await asyncio.gather(*updates, third, waiting)
        self.assertEqual(applies, 3)
        self.assertEqual(len(self.root.registry().fibers(definition)), 1)
        outer = self.fiber("x").parent()
        assert outer is not None
        self.assertEqual(list(outer.inject().keys()), ["qux"])
