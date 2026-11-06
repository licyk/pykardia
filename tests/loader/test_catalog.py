"""Catalog identity, alias snapshots and resolution timing."""

import unittest

from pykardia import Context, JsonValue, PluginDef
from pykardia.loader.catalog import Catalog, ModuleNotFound
from pykardia.timer import TimerPlugin


async def apply(ctx: Context, config: JsonValue) -> None:
    """Provide an inert definition for identity-only catalog scenarios."""


class CatalogTests(unittest.IsolatedAsyncioTestCase):
    """Verify actual plugin definitions and already-resolved asynchronous observers."""

    async def test_register_alias_and_snapshot_resolution(self) -> None:
        """Existing aliases and resolve observers keep their original plugin identity."""
        catalog = Catalog.new()
        first, second = PluginDef.new(apply), PluginDef.new(apply)
        self.assertIs(catalog.register_def("plugin", first), catalog)
        self.assertIs(catalog.alias("alias", "plugin"), catalog)
        pending = catalog.resolve("plugin")
        self.assertTrue(pending.is_ready())
        catalog.register_def("plugin", second)
        self.assertIs(await pending, first)
        self.assertIs(await catalog.resolve("plugin"), second)
        self.assertIs(await catalog.resolve("alias"), first)
        self.assertIsNone(catalog.get("missing"))
        catalog.alias("alias", "missing")
        self.assertIs(catalog.get("alias"), first)

    async def test_missing_resolution_and_typed_registration(self) -> None:
        """Missing names retain the failure captured before a later registration."""
        catalog = Catalog()
        pending = catalog.resolve("timer")
        catalog.register(TimerPlugin)
        with self.assertRaisesRegex(ModuleNotFound, "Cannot find plugin 'timer'") as caught:
            await pending
        self.assertEqual(caught.exception.name, "timer")
        self.assertEqual((await catalog.resolve("timer")).key(), PluginDef.of(TimerPlugin).key())
