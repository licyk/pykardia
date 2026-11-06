"""Real files, loader and include fixtures for integration acceptance."""

import tempfile
import unittest
from pathlib import Path

import async_timeout

from pykardia import Context, JsonValue, PluginDef
from pykardia.fs import FsPlugin
from pykardia.include import Include, IncludePlugin
from pykardia.loader import Loader, builtins
from tests.loader.common import Counter


class IncludeCase(unittest.IsolatedAsyncioTestCase):
    """Install actual services and a mutable counting plugin catalog."""

    async def asyncSetUp(self) -> None:
        """Create a private directory and the actual fs/loader/plugin runtime."""
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = Context()
        self.catalog = builtins()
        self.catalog.register(IncludePlugin)
        self.counter = Counter()
        self.catalog.register_def("worker", self.counter.definition())
        await self.root.plugin(FsPlugin, None).settled()
        await self.root.plugin_def(Loader.plugin(self.catalog)).settled()
        self.loader = self.root.get(Loader).arc()

    async def asyncTearDown(self) -> None:
        """Flush real includes and dispose native watchers before removing the directory."""
        async with async_timeout.timeout(8):
            await self.root.fiber().dispose()
        self.temp.cleanup()

    async def boot(
        self,
        name: str,
        text: str | None,
        config: dict[str, JsonValue] | None = None,
        definition: PluginDef | None = None,
    ) -> Include:
        """Mount a file through a real loader entry and return its owning include."""
        path = self.base / name
        if text is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(text.encode())
        if definition is not None:
            self.catalog.register_def("include", definition)
        options: JsonValue = {
            "id": "inc",
            "name": "include",
            "config": {"path": str(path), **(config or {})},
        }
        async with async_timeout.timeout(8):
            await self.loader.create(options)
            await self.loader.settled()
        entry = self.loader.resolve("inc")
        fiber = entry.inner_fiber()
        assert fiber is not None
        if fiber.error() is not None:
            raise AssertionError(str(fiber.error()))
        tree = entry.subtree()
        assert tree is not None
        include = Include.of(tree)
        assert include is not None
        return include
