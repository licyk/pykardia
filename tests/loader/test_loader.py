"""Source tree reconciliation and plugin-driven persistence scenarios."""

from __future__ import annotations

import asyncio

from pykardia import Context, FiberState, JsonValue, PluginDef
from pykardia.loader import EntryValidationError
from tests.loader.common import LoaderCase


class LoaderTests(LoaderCase):
    """Run real entry fibers and verify source data and backend notifications."""

    async def test_basic_support(self) -> None:
        """Reconcile nested groups, then persist a plugin's own update and disposal."""
        foo, bar, qux = self.register("foo"), self.register("bar"), self.register("qux")
        await self.read(
            [
                {"id": "1", "name": "foo"},
                {
                    "id": "2",
                    "name": "group",
                    "group": True,
                    "config": [
                        {"id": "3", "name": "bar", "config": {"a": 1}},
                        {"id": "4", "name": "qux", "disabled": True},
                    ],
                },
            ]
        )
        self.assertEqual((foo.applied, bar.applied, qux.applied), (1, 1, 0))
        await self.read([{"id": "1", "name": "foo"}, {"id": "4", "name": "qux"}])
        self.assertEqual((foo.applied, bar.disposed, qux.applied), (1, 1, 1))
        await self.fiber("1").update({"a": 3})
        self.assertEqual(
            self.loader.root().data(),
            [
                {"id": "1", "name": "foo", "config": {"a": 3}},
                {"id": "4", "name": "qux"},
            ],
        )
        await self.fiber("1").dispose()
        self.assertEqual(
            self.loader.root().data(),
            [
                {"id": "1", "name": "foo", "disabled": True, "config": {"a": 3}},
                {"id": "4", "name": "qux"},
            ],
        )

    async def test_entry_failure_and_invalid_source_recovery(self) -> None:
        """A bad entry neither cancels siblings nor survives invalid options under its ID."""

        async def fail(ctx: Context, config: JsonValue) -> None:
            """Produce an actual plugin apply failure."""
            raise ValueError("boom")

        self.catalog.register_def("bad", PluginDef.new(fail))
        good = self.register("good")
        await self.read([{"id": "1", "name": "bad"}, {"id": "2", "name": "good"}])
        self.assertEqual(self.fiber("1").state(), FiberState.Failed)
        self.assertEqual(self.fiber("2").state(), FiberState.Active)
        await self.read([{"id": "2", "name": 1}])
        self.assertEqual(good.disposed, 1)
        with self.assertRaisesRegex(ValueError, "cannot resolve entry 2"):
            self.loader.resolve("2")
        await self.read([{"id": "2", "name": "good"}])
        self.assertEqual(good.applied, 2)

    async def test_commit_reports_changes_and_moves(self) -> None:
        """Runtime mutation reports create/update/move/remove, while source reads do not commit."""
        self.register("foo")
        await self.read([{"id": "g", "name": "group", "group": True, "config": []}])
        self.assertEqual(self.backend.changes, [])
        await self.loader.create({"id": "a", "name": "foo"})
        await self.loader.settled()
        original = self.fiber("a")
        await self.loader.update("a", {"custom": 1}, parent="g")
        await self.loader.settled()
        self.assertEqual(self.fiber("a"), original)
        self.assertEqual(self.loader.resolve("a").parent().owner(), self.loader.resolve("g"))
        await self.loader.update("a", {"custom": None}, parent=None, position=0)
        self.assertEqual(self.loader.root().data()[0], {"id": "a", "name": "foo"})
        self.loader.remove("a")
        await self.loader.settled()
        self.assertEqual([change.id for change in self.backend.changes], ["a"] * 4)
        self.assertIsNone(self.backend.changes[0].legacy)
        self.assertEqual(self.backend.changes[1].from_, self.loader.root())
        self.assertIsNone(self.backend.changes[-1].options)

    async def test_runtime_validation_before_commit(self) -> None:
        """Deep runtime validation leaves both source and backend untouched on failure."""
        self.register("foo")
        with self.assertRaises(EntryValidationError):
            await self.loader.create({"name": "group", "group": True, "config": [{"name": 1}]})
        self.assertEqual(self.backend.changes, [])
        self.assertEqual(self.loader.root().data(), [])
        await self.loader.create({"id": "a", "name": "foo"})
        with self.assertRaises(EntryValidationError):
            await self.loader.update("a", {"name": None})
        self.assertEqual(len(self.backend.changes), 1)
        self.assertEqual(self.loader.resolve("a").options(), {"id": "a", "name": "foo"})

    async def test_awaits_loader_barrier(self) -> None:
        """Await metadata blocks until current loads settle while ordinary pending fibers do not."""
        gate, entered = asyncio.Event(), asyncio.Event()

        async def slow(ctx: Context, config: JsonValue) -> None:
            """Keep a real inner fiber loading until the scenario releases it."""
            entered.set()
            await gate.wait()

        self.catalog.register_def("slow", PluginDef.new(slow))
        self.catalog.register_def("pending", PluginDef.new(slow).inject(["never"]))
        self.register("last")
        await self.loader.create({"id": "a", "name": "slow"})
        await entered.wait()
        await self.loader.create({"id": "b", "name": "pending"})
        await self.loader.create({"id": "c", "name": "last", "inject": {"loader": {"await": True}}})
        outer = self.loader.resolve("c").outer_fiber()
        assert outer is not None
        await outer.settled()
        self.assertEqual(outer.state(), FiberState.Pending)
        self.assertIsNone(self.loader.resolve("c").inner_fiber())
        gate.set()
        await self.loader.settled()
        self.assertEqual(self.fiber("c").state(), FiberState.Active)
        self.assertEqual(self.fiber("b").state(), FiberState.Pending)

    async def test_group_update_keeps_group_identity(self) -> None:
        """A group handles config updates in place and settles actual child reconciliation."""
        foo = self.register("foo")
        await self.read([{"id": "g", "name": "group", "group": True, "config": []}])
        group_fiber = self.fiber("g")
        await self.loader.update("g", {"config": [{"id": "a", "name": "foo"}]})
        await self.loader.settled()
        self.assertEqual(self.fiber("g"), group_fiber)
        self.assertEqual(foo.applied, 1)
        self.assertEqual(
            self.loader.resolve("g").options(),
            {"id": "g", "name": "group", "group": True, "config": [{"id": "a", "name": "foo"}]},
        )
