"""Actual guest artifact, loader and HMR fixtures shared by the three transports."""

import os
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

import async_timeout

from pykardia import Context, JsonValue
from pykardia.hmr import Hmr, HmrExt, ModuleGraph, ModuleSource, ReconcileResult
from pykardia.hmr.guest import GuestService
from pykardia.loader import EntryHandle, Loader
from tests.parity.rust_guest.build import Kind, artifact


class GuestCase(unittest.IsolatedAsyncioTestCase):
    """Drive original guest artifacts through the actual graph, bridge and loader."""

    kind: ClassVar[Kind]
    Source: ClassVar[type[ModuleSource]]

    @classmethod
    def setUpClass(cls) -> None:
        """Compile both source fixture versions before entering asynchronous tests."""
        artifact(cls.kind, "v1")
        artifact(cls.kind, "v2")

    async def asyncSetUp(self) -> None:
        """Create a private executable path and real module/HMR services."""
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.path = self.base / artifact(self.kind, "v1").name
        self.root = Context()
        self.graph = ModuleGraph(self.Source())
        await self.root.plugin_def(Loader.plugin(self.graph)).settled()
        await self.root.plugin_def(Hmr.plugin(self.graph)).settled()
        self.loader = self.root.get(Loader).arc()
        self.hmr = HmrExt(self.root.get(Hmr))

    async def asyncTearDown(self) -> None:
        """Dispose real instances and reap every child before removing the fixture directory."""
        async with async_timeout.timeout(8):
            await self.root.fiber().dispose()
        self.temp.cleanup()

    def install(self, version: str) -> None:
        """Replace the executable atomically without opening a running inode for writing."""
        staged = self.base / "replacement"
        os.link(artifact(self.kind, version), staged)
        staged.replace(self.path)

    async def load(self, config: JsonValue = None) -> EntryHandle:
        """Start a real plugin from the installed executable and require successful application."""
        if not self.path.exists():
            self.install("v1")
        id = await self.loader.create({"name": str(self.path), "config": config})
        await self.loader.settled()
        entry = self.loader.resolve(id)
        fiber = entry.inner_fiber()
        assert fiber is not None
        await fiber.settled()
        return entry

    def service(self) -> GuestService:
        """Access the actual service object provided by the guest callback."""
        return self.root.get_as("greeter", GuestService).arc()

    async def reload(self) -> ReconcileResult:
        """Reload the changed executable through a genuine HMR transaction."""

        async def roots() -> list[Path]:
            """Report the installed executable as the transaction's changed root."""
            return [self.path]

        return await self.hmr.reconcile(roots)
