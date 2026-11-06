"""Source watch.rs acceptance with the native filesystem observer."""

import asyncio

import async_timeout

from pykardia.include import Include
from tests.include.common import IncludeCase


class WatchTests(IncludeCase):
    """Exercise default, restarted and rename-surviving file subscriptions."""

    async def external_edit(self, include: Include) -> None:
        """Write real changes until the native event reaches the running plugin."""
        async with async_timeout.timeout(5):
            value = 100
            while not self.counter.configs or self.counter.configs[-1] != value:
                value += 1
                include.filename().write_text(f"- id: a\n  name: worker\n  config: {value}\n")
                await asyncio.sleep(0.1)

    async def test_does_not_watch_the_config_file_by_default(self) -> None:
        """Repeated external changes stay invisible until an explicit refresh."""
        include = await self.boot("default.yml", "- id: a\n  name: worker\n  config: 0\n")
        for value in range(100, 110):
            include.filename().write_text(f"- id: a\n  name: worker\n  config: {value}\n")
            await asyncio.sleep(0.1)
        self.assertEqual(self.counter.configs, [0])

    async def test_watches_the_config_file_with_watch_true(self) -> None:
        """A real native event applies external data without requesting refresh."""
        include = await self.boot(
            "watch.yml",
            "- id: a\n  name: worker\n  config: 0\n",
            {"watch": True},
        )
        await self.external_edit(include)
        self.assertGreater(self.counter.applied, 1)

    async def test_restarts_when_watch_is_toggled(self) -> None:
        """Changing watch restarts the include and installs a working subscription."""
        include = await self.boot("toggle.yml", "- id: a\n  name: worker\n  config: 0\n")
        await self.loader.update(
            "inc", {"config": {"path": str(include.filename()), "watch": True}}
        )
        await self.loader.settled()
        tree = self.loader.resolve("inc").subtree()
        assert tree is not None
        restarted = Include.of(tree)
        assert restarted is not None
        self.assertIsNot(restarted, include)
        await self.external_edit(restarted)

    async def test_keeps_watching_the_file_across_its_own_rename_based_writes(self) -> None:
        """Two atomic writes preserve native watches and do not create a feedback loop."""
        include = await self.boot(
            "rename.yml",
            "- id: a\n  name: worker\n  config: 0\n",
            {"watch": True},
        )
        for value in [1, 2]:
            await include.tree().update("a", {"config": value})
            await include.refresh()
        self.assertIn("config: 2", include.filename().read_text())
        await asyncio.sleep(0.5)
        await self.external_edit(include)
        await include.flush()
        external = include.filename().read_text()
        applied = self.counter.applied
        await asyncio.sleep(0.5)
        self.assertEqual(include.filename().read_text(), external)
        self.assertEqual(self.counter.applied, applied)
