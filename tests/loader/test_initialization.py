"""Initial-import and overlapping rebuild boundaries from the reference loader suite."""

import asyncio
from collections.abc import Awaitable

from pykardia import Context, Disposer, JsonValue, PluginDef
from pykardia.loader import EntryHandle, EntryOptions, value_at
from tests.loader.common import Hold, LoaderCase


class InitializationTests(LoaderCase):
    """Gate actual imports, patch continuations and disposer completion."""

    async def test_rename_during_initial_import_and_patch(self) -> None:
        """Initial creation joins a rename and never applies the obsolete definition."""
        for boundary in ("import", "patch"):
            old, new = self.register("old"), self.register("new")
            entered, release = asyncio.Event(), asyncio.Event()
            handle = None
            if boundary == "import":
                hold = Hold("old")
                self.resolver.hold = hold
                entered, release = hold.entered, hold.release
            else:

                async def hook(
                    entry: EntryHandle,
                    next_: Awaitable[None],
                    entered: asyncio.Event = entered,
                    release: asyncio.Event = release,
                ) -> None:
                    """Pause the original entry patch before it can start a fiber."""
                    if EntryOptions.read(entry.options()).name == "old":
                        entered.set()
                        await release.wait()
                    await next_

                handle = self.loader.on_patch_context(self.root, hook)
            creating = asyncio.create_task(self.loader.create({"id": "x", "name": "old"}))
            await entered.wait()
            changing = asyncio.create_task(self.loader.update("x", {"name": "new"}))
            while EntryOptions.read(self.loader.resolve("x").options()).name != "new":
                await asyncio.sleep(0)
            release.set()
            await asyncio.gather(creating, changing)
            await self.loader.settled()
            self.assertEqual((old.applied, new.applied), (0, 1))
            self.loader.remove("x")
            await self.loader.settled()
            if handle is not None:
                await handle.dispose()

    async def test_two_inject_updates_join_one_disposal(self) -> None:
        """A pending rebuild uses the latest inject and produces only one new episode."""
        self.root.provide_as("foo", 1)
        self.root.provide_as("bar", 2)
        entered, release = asyncio.Event(), asyncio.Event()
        applications: list[Context] = []

        async def apply(ctx: Context, config: JsonValue) -> None:
            """Retain real contexts and block the first episode's cleanup."""
            applications.append(ctx)

            async def cleanup() -> None:
                """Wait at the actual inverse boundary."""
                entered.set()
                await release.wait()

            ctx.defer("gate", Disposer.future(cleanup))

        self.catalog.register_def("plug", PluginDef.new(apply))
        await self.read([{"id": "x", "name": "plug"}])
        self.backend.changes.clear()
        first = asyncio.create_task(self.loader.update("x", {"inject": ["foo"]}))
        await entered.wait()
        second = asyncio.create_task(self.loader.update("x", {"inject": ["bar"]}))
        while value_at(self.loader.resolve("x").options(), ["inject"]) != ["bar"]:
            await asyncio.sleep(0)
        self.assertEqual(len(applications), 1)
        self.assertTrue(self.loader.is_busy())
        release.set()
        await asyncio.gather(first, second)
        await self.loader.settled()
        self.assertEqual(len(applications), 2)
        self.assertEqual(applications[-1].get_as("bar", int).arc(), 2)
        self.assertTrue(
            all(
                isinstance(c.options, dict) and "disabled" not in c.options
                for c in self.backend.changes
            )
        )

    async def test_isolation_without_outer_cuts_parent_injection(self) -> None:
        """A child isolation blocks inherited injection even without its own outer fiber."""
        counter = self.register("worker")
        self.root.provide_as("settings", {"enabled": True})
        await self.read(
            [
                {
                    "id": "g",
                    "name": "group",
                    "group": True,
                    "inject": ["settings"],
                    "config": [
                        {
                            "id": "x",
                            "name": "worker",
                            "isolate": {"settings": "other"},
                            "disabled": {"__jsExpr": "!settings.enabled"},
                        }
                    ],
                }
            ]
        )
        entry = self.loader.resolve("x")
        self.assertIsNone(entry.outer_fiber())
        self.assertTrue(entry.is_disabled())
        self.assertEqual(counter.applied, 0)
