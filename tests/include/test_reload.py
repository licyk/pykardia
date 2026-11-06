"""Source reload.rs scenarios using real include entries and source files."""

from pykardia import Level
from pykardia.include import Include
from tests.include.common import IncludeCase


class ReloadTests(IncludeCase):
    """Verify cache recovery, source changes and patch updates without replacing includes."""

    async def test_re_applies_patches_on_every_reload(self) -> None:
        """Every parse overlays the current patches before the loader sees entries."""
        include = await self.boot(
            "reload.yml",
            "- id: a\n  name: worker\n  config: 1\n",
            {"patches": [{"id": "a", "disabled": True}]},
        )
        self.assertEqual(self.counter.applied, 0)
        include.filename().write_text(
            "- id: a\n  name: worker\n  config: 1\n- id: x\n  name: worker\n  config: 2\n"
        )
        await include.refresh()
        await self.loader.settled()
        self.assertEqual(self.counter.configs, [2])

    async def test_rebuilds_an_entry_whose_name_changes_in_the_file(self) -> None:
        """Changing the plugin into a group retires its old instance without writing disabled."""
        include = await self.boot("rename.yml", "- id: a\n  name: worker\n  config: 1\n")
        renamed = "- id: a\n  name: group\n  group: true\n  config: []\n"
        include.filename().write_text(renamed)
        await include.refresh()
        await self.loader.settled()
        self.assertEqual(self.counter.disposed, 1)
        self.assertIsNotNone(include.tree().resolve("a").subgroup())
        self.assertEqual(include.filename().read_text(), renamed)

    async def test_reverts_an_entry_when_its_patch_is_removed(self) -> None:
        """Removing patches restores the parsed original while keeping the include instance."""
        include = await self.boot(
            "revert.yml",
            "- id: a\n  name: worker\n  config: 1\n",
            {"patches": [{"id": "a", "disabled": True}]},
        )
        self.assertEqual(self.counter.applied, 0)
        await self.loader.update("inc", {"config": {"path": str(include.filename())}})
        await self.loader.settled()
        tree = self.loader.resolve("inc").subtree()
        assert tree is not None
        self.assertIs(Include.of(tree), include)
        self.assertEqual(self.counter.configs, [1])

    async def test_keeps_the_tree_reloadable_after_an_invalid_file(self) -> None:
        """A bad parse keeps the last good tree and later valid input can extend it."""
        include = await self.boot("invalid.yml", "- id: a\n  name: worker\n  config: 1\n")
        include.filename().write_text("a: 1\n")
        await include.refresh()
        self.assertTrue(
            any(
                log.level is Level.Warn and "failed to validate config file" in log.text
                for log in self.root.log_buffer()
            )
        )
        self.assertEqual(self.counter.disposed, 0)
        include.filename().write_text(
            "- id: a\n  name: worker\n  config: 1\n- id: x\n  name: worker\n  config: 2\n"
        )
        await include.refresh()
        await self.loader.settled()
        self.assertEqual(self.counter.configs, [1, 2])
        self.assertEqual(self.counter.disposed, 0)

    async def test_never_overwrites_an_existing_but_unparsable_file_with_initial(self) -> None:
        """Only a missing file permits initial fallback; broken existing data survives."""
        broken = "- {\n"
        path = self.base / "broken.yml"
        path.write_text(broken)
        await self.loader.create(
            {
                "id": "inc",
                "name": "include",
                "config": {
                    "path": str(path),
                    "initial": [{"id": "x", "name": "worker", "config": 1}],
                },
            }
        )
        await self.loader.settled()
        self.assertEqual(path.read_text(), broken)
        self.assertEqual(self.counter.applied, 0)
        fiber = self.loader.resolve("inc").inner_fiber()
        assert fiber is not None
        self.assertIn("failed to parse config file", str(fiber.error()))

    async def test_writes_initial_for_a_missing_file(self) -> None:
        """Initial anonymous entries receive persistent IDs before loading."""
        include = await self.boot(
            "missing.yml",
            None,
            {"initial": [{"name": "worker", "config": 1}]},
        )
        self.assertEqual(self.counter.configs, [1])
        self.assertIn("id: ", include.filename().read_text())
        self.assertEqual(len(include.tree().entries()), 1)
