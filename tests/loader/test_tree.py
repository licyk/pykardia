"""Nested tree ownership, transfer commits and retained entry identities."""

from __future__ import annotations

import asyncio

from pykardia import Context, Disposer, JsonValue, PluginDef
from pykardia.loader import TreeHandle
from tests.loader.common import LoaderCase, Recorded


class TreeTests(LoaderCase):
    """Drive transfers through actual tree plugins and backend observers."""

    async def asyncSetUp(self) -> None:
        """Install a tree-owning plugin with a target group and separate persistence sink."""
        await super().asyncSetUp()
        self.counter = self.register("foo")
        self.target_backend = Recorded()
        self.trees: list[TreeHandle] = []

        async def apply(ctx: Context, config: JsonValue) -> None:
            """Create a real nested tree and retain its stop effect on the owning plugin."""
            tree = self.loader.create_tree(ctx, self.target_backend)
            self.trees.append(tree)
            ctx.defer("tree", Disposer.new(tree.root().stop))
            await tree.root().update([{"id": "g", "name": "group", "group": True, "config": []}])

        self.catalog.register_def("tree", PluginDef.new(apply).inject(["loader"]))
        await self.loader.create({"id": "tree", "name": "tree"})
        await self.loader.settled()
        self.backend.changes.clear()

    async def test_cross_tree_move_reports_removal_and_creation(self) -> None:
        """Source and destination backends receive separate records with the same local ID."""
        await self.loader.create({"id": "x", "name": "foo", "config": {"a": 1}})
        await self.loader.settled()
        self.backend.changes.clear()
        await self.loader.update("x", {"config": {"a": 2}}, parent="tree:g")
        await self.loader.settled()
        self.assertEqual(len(self.backend.changes), 1)
        self.assertEqual(len(self.target_backend.changes), 1)
        removal, creation = self.backend.changes[0], self.target_backend.changes[0]
        self.assertEqual(removal.group, self.loader.root())
        self.assertIsNone(removal.options)
        self.assertEqual(removal.legacy, {"id": "x", "name": "foo", "config": {"a": 1}})
        self.assertEqual(creation.group, self.loader.resolve("tree:g").subgroup())
        self.assertIsNone(creation.legacy)
        self.assertIsNone(creation.from_)
        self.assertEqual(creation.options, {"id": "x", "name": "foo", "config": {"a": 2}})

    async def test_transfers_entry_and_group_members(self) -> None:
        """Moving across trees retains handles and fibers while changing composite IDs."""
        await self.loader.create({"id": "x", "name": "foo"})
        await self.loader.settled()
        entry, fiber = self.loader.resolve("x"), self.fiber("x")
        await self.loader.update("x", {}, parent="tree:g")
        await self.loader.settled()
        self.assertEqual(self.loader.resolve("tree:x"), entry)
        self.assertEqual(entry.id(), "tree:x")
        self.assertEqual(self.fiber("tree:x"), fiber)
        with self.assertRaises(ValueError):
            self.loader.resolve("x")
        group = self.loader.resolve("tree:g").subgroup()
        assert group is not None
        await group.update([entry.options()])
        await self.loader.settled()
        self.assertEqual(self.loader.resolve("tree:x"), entry)
        self.assertEqual(self.counter.applied, 1)
        self.loader.remove("tree:x")
        await self.loader.settled()
        await self.loader.create(
            {
                "id": "bundle",
                "name": "group",
                "group": True,
                "config": [{"id": "member", "name": "foo"}],
            }
        )
        await self.loader.settled()
        member, inner = self.loader.resolve("member"), self.fiber("member")
        await self.loader.update("bundle", {}, parent="tree:g")
        await self.loader.settled()
        self.assertEqual(self.loader.resolve("tree:member"), member)
        self.assertEqual(self.fiber("tree:member"), inner)
        self.assertEqual(member.id(), "tree:member")
        with self.assertRaises(ValueError):
            self.loader.resolve("member")
        self.loader.remove("tree:bundle")
        await self.loader.settled()
        self.assertEqual(self.counter.disposed, 2)

    async def test_rejects_duplicate_target_before_commit(self) -> None:
        """A conflicting local ID leaves entry ownership, source data and both backends intact."""
        await self.loader.create({"id": "g", "name": "foo"})
        await self.loader.settled()
        entry = self.loader.resolve("g")
        before = self.loader.root().data()
        self.backend.changes.clear()
        with self.assertRaisesRegex(ValueError, "entry g already exists in the target tree"):
            await self.loader.update("g", {}, parent="tree:g")
        self.assertEqual(self.loader.resolve("g"), entry)
        self.assertEqual(entry.parent(), self.loader.root())
        self.assertEqual(self.loader.root().data(), before)
        self.assertEqual(self.backend.changes, [])
        self.assertEqual(self.target_backend.changes, [])

    async def test_loader_loss_does_not_persist_child_disposal(self) -> None:
        """Delayed tree stop during ancestor unload cannot write disabled into its source."""
        backend = Recorded()
        trees: list[TreeHandle] = []

        async def apply(ctx: Context, config: JsonValue) -> None:
            """Own a nested tree whose stop runs after an asynchronous flush boundary."""
            tree = self.loader.create_tree(ctx, backend)
            trees.append(tree)
            await tree.root().update([{"id": "child", "name": "foo"}])

            async def cleanup() -> None:
                """Model the source include's flush-before-stop asynchronous boundary."""
                await asyncio.sleep(0)
                tree.root().stop()

            ctx.defer("tree", Disposer.future(cleanup))

        fiber = self.loader.context().plugin_def(PluginDef.new(apply).inject(["loader"]))
        await fiber.settled()
        await self.loader.settled()
        await self.loader.context().fiber().dispose()
        self.assertEqual(trees[0].root().data(), [{"id": "child", "name": "foo"}])
        self.assertEqual(backend.changes, [])
