"""Real include startup, synchronization, source identity and write recovery."""

import asyncio
import errno
import os
from pathlib import Path

import async_timeout

from pykardia import JsonValue
from pykardia.include import Include, IncludePlugin
from pykardia.include.yaml import parse
from tests.include.common import IncludeCase


class IncludeTests(IncludeCase):
    """Drive file and runtime edits through actual plugin and tree APIs."""

    async def test_initial_and_runtime_writeback(self) -> None:
        """Missing files use initial data and later runtime edits write deterministic YAML."""
        include = await self.boot(
            "initial.yml", None, {"initial": [{"id": "a", "name": "worker", "config": 1}]}
        )
        await include.tree().update("a", {"config": 2, "custom": True})
        await include.flush()
        self.assertEqual(
            parse(include.filename().read_text())[0],
            [{"id": "a", "name": "worker", "custom": True, "config": 2}],
        )
        self.assertEqual(self.counter.configs, [1, 2])
        self.assertEqual(list(self.base.glob("*.tmp")), [])

    async def test_read_and_anonymous_identity(self) -> None:
        """Editing a neighboring entry reuses unchanged anonymous source IDs and fibers."""
        include = await self.boot(
            "anonymous.yml",
            "- name: worker\n  config: 1\n- id: named\n  name: worker\n  config: 2\n",
        )
        anonymous = next(
            entry for entry in include.tree().entries() if not entry.id().endswith("named")
        )
        fiber = anonymous.inner_fiber()
        include.filename().write_text(
            "- name: worker\n  config: 1\n- id: named\n  name: worker\n  config: 3\n"
        )
        await include.refresh()
        await self.loader.settled()
        self.assertEqual(anonymous.inner_fiber(), fiber)
        self.assertEqual(self.counter.applied, 3)
        self.assertNotIn("id:", include.filename().read_text().split("- id:")[0])

    async def test_readonly_conflict_file_wins_and_independent_change_survives(self) -> None:
        """Pending edits survive read-only writes, then reconcile against external file changes."""
        include = await self.boot("conflict.yml", "- id: a\n  name: worker\n  config: 1\n")
        include.filename().chmod(0o444)
        await include.tree().update("a", {"config": 2, "custom": "kept"})
        await include.flush()
        self.assertIn("config: 1", include.filename().read_text())
        include.filename().chmod(0o644)
        include.filename().write_text("- id: a\n  name: worker\n  config: 3\n")
        await include.refresh()
        await self.loader.settled()
        self.assertEqual(
            parse(include.filename().read_text())[0],
            [{"id": "a", "name": "worker", "config": 3, "custom": "kept"}],
        )
        self.assertTrue(any("file wins" in log.text for log in self.root.log_buffer()))

    async def test_read_error_suspends_write_until_repaired(self) -> None:
        """An invalid file preserves the old runtime and pending edits until a successful read."""
        include = await self.boot("repair.yml", "- id: a\n  name: worker\n  config: 1\n")
        include.filename().write_text("[")
        await include.tree().update("a", {"custom": "pending"})
        await include.flush()
        self.assertEqual(include.filename().read_text(), "[")
        self.assertTrue(include._state.journal)
        include.filename().write_text("- id: a\n  name: worker\n  config: 1\n")
        await include.refresh()
        self.assertIn("custom: pending", include.filename().read_text())

    async def test_rename_retry_and_failed_batch_recovery(self) -> None:
        """A transient replacement retries; a permanent failure retains records for later writes."""
        attempts: list[int] = []
        fail = True

        def rename(source: Path, target: Path) -> None:
            """Inject only the external rename boundary, leaving batching and recovery real."""
            attempts.append(1)
            if len(attempts) == 1:
                raise PermissionError(errno.EACCES, "busy")
            if fail:
                raise OSError(errno.EIO, "disk failure")
            os.replace(source, target)

        include = await self.boot(
            "retry.yml",
            "- id: a\n  name: worker\n  config: 1\n",
            definition=IncludePlugin.with_rename(rename),
        )
        await include.tree().update("a", {"config": 2})
        await include.flush()
        self.assertEqual(len(attempts), 2)
        self.assertTrue(include._state.journal)
        self.assertFalse(list(self.base.glob(".*.tmp")))
        fail = False
        await include.tree().update("a", {"custom": True})
        await include.flush()
        self.assertEqual(
            parse(include.filename().read_text())[0],
            [{"id": "a", "name": "worker", "config": 2, "custom": True}],
        )
        self.assertFalse(include._state.journal)

    async def test_patch_update_keeps_include_and_nested_write_routes_to_parent(self) -> None:
        """Patch-owned config writes to its parent include source, preserving the child file."""
        child = self.base / "child.yml"
        child.write_text("- id: a\n  name: worker\n  config: 1\n")
        include = await self.boot(
            "parent.yml",
            "- id: child\n  name: include\n  config:\n    path: child.yml\n"
            "    patches:\n    - id: a\n      config: 2\n",
        )
        entry = include.tree().resolve("child")
        tree = entry.subtree()
        assert tree is not None
        nested = Include.of(tree)
        assert nested is not None
        original = entry.inner_fiber()
        await tree.update("a", {"config": 3})
        await nested.flush()
        await include.flush()
        await self.loader.settled()
        self.assertEqual(entry.inner_fiber(), original)
        self.assertIn("config: 3", include.filename().read_text())
        self.assertIn("config: 1", child.read_text())

    async def test_real_watch_refresh_and_shutdown_preserves_source(self) -> None:
        """Native replacement refreshes the tree; shutdown adds no disabled fields."""
        include = await self.boot(
            "watch.yml", "- id: a\n  name: worker\n  config: 1\n", {"watch": True}
        )
        temporary = self.base / "replacement"
        temporary.write_text("- id: a\n  name: worker\n  config: 2\n")
        temporary.replace(include.filename())
        async with async_timeout.timeout(5):
            while self.counter.configs[-1] != 2:
                await asyncio.sleep(0.01)
        await include.tree().update("a", {"custom": "last"})
        await self.root.fiber().dispose()
        written = include.filename().read_text()
        self.assertIn("custom: last", written)
        self.assertNotIn("disabled", written)

    async def test_json_format_and_written_value_locations(self) -> None:
        """JSON writeback uses two-space output and only locates values still written in source."""
        include = await self.boot("config.json", '[{"id":"a","name":"worker","config":1}]')
        location = include.tree().locate("a", ["config"], 1)
        self.assertIsNotNone(location)
        self.assertIsNone(include.tree().locate("a", ["config"], 2))
        patch: JsonValue = {"config": {"x": 1e-5}}
        await include.tree().update("a", patch)
        await include.flush()
        self.assertEqual(
            include.filename().read_text(),
            '[\n  {\n    "id": "a",\n    "name": "worker",\n'
            '    "config": {\n      "x": 0.00001\n    }\n  }\n]',
        )
