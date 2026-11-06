"""Port all eleven HMR watch source cases using the production native filesystem watcher."""

import asyncio
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from pathlib import Path

import async_timeout

from pykardia import Context, JsonValue
from pykardia.fs import FsPlugin
from pykardia.hmr import Exports, Hmr, HmrExt, Imports, ModuleCode, ModuleGraph
from pykardia.hmr.watch import batches
from pykardia.loader import Catalog, Loader, TreeBackend
from tests.hmr.common import Query, dependent, plugin, throws, value
from tests.hmr.test_graph import MemorySource


class FileTree(TreeBackend):
    """Supply a real loader tree's configuration directory."""

    def __init__(self, directory: Path) -> None:
        """Retain the directory resolved by the HMR entry."""
        self.directory = directory

    def base_dir(self) -> Path:
        """Return the source tree's directory."""
        return self.directory


class WatchTests(unittest.IsolatedAsyncioTestCase):
    """Exercise actual event filtering, debounce, transactions and failed-batch retries."""

    async def asyncSetUp(self) -> None:
        """Create writable files and an actual module source before enabling watching."""
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = Context()
        self.root.set_log_buffer_size(1000)
        self.source = MemorySource()
        self.graph = ModuleGraph(self.source)

    async def asyncTearDown(self) -> None:
        """Stop every watcher, task and plugin before removing its directory."""
        async with async_timeout.timeout(8):
            await self.root.fiber().dispose()
        self.temp.cleanup()

    async def setup_app(self, watch: JsonValue = None) -> Loader:
        """Install the real loader, fs provider and verbose HMR watch."""
        await self.root.plugin_def(Loader.plugin(self.graph)).settled()
        await self.root.plugin(FsPlugin, None).settled()
        config: JsonValue = {
            "base": str(self.base),
            "watch": {"enabled": True, "debounce": 100} if watch is None else watch,
        }
        await (
            self.root.intercept("logger", {"level": 3})
            .plugin_def(Hmr.plugin(self.graph), config)
            .settled()
        )
        return self.root.get(Loader).arc()

    def write(self, name: str, code: ModuleCode) -> Path:
        """Replace module code and modify its actual watched file."""
        path = self.base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        self.source.codes[str(path)] = code
        path.write_text(str(time.monotonic_ns()))
        return path

    async def load(self, loader: Loader, *paths: Path) -> None:
        """Load current module versions through actual loader entries."""
        for path in paths:
            await loader.create({"name": str(path)})
        await loader.settled()

    def query(self, name: str) -> JsonValue:
        """Read the current episode using its scoped event listener."""
        return self.root.bail(Query(name))

    def logs(self) -> list[str]:
        """Collect logs from the actual runtime buffer."""
        return [message.text for message in self.root.log_buffer()]

    async def eventually(self, check: Callable[[], bool]) -> None:
        """Wait for real OS dispatch and transaction completion with a bounded deadline."""
        async with async_timeout.timeout(5):
            while not check():
                await asyncio.sleep(0.02)

    async def base_without_entry(self, config: JsonValue) -> Path:
        """Instantiate HMR directly and inspect its resolved base directory."""
        await self.root.plugin_def(Hmr.plugin(self.graph), config).settled()
        return self.root.get(Hmr).arc().base_dir()

    async def base_of_entry(self, config: JsonValue) -> Path:
        """Resolve HMR inside a loader entry with a file-backed tree directory."""
        catalog = Catalog()
        catalog.register_def("hmr", Hmr.plugin(self.graph))
        await self.root.plugin_def(Loader.plugin_with(catalog, FileTree(self.base))).settled()
        loader = self.root.get(Loader).arc()
        await loader.create({"name": "hmr", "config": config})
        await loader.settled()
        return self.root.get(Hmr).arc().base_dir()

    async def test_defaults_to_the_working_directory_without_a_context_directory(self) -> None:
        """Direct HMR without a base uses the process working directory."""
        self.assertEqual(await self.base_without_entry({}), Path.cwd())

    async def test_accepts_a_relative_path_without_a_context_directory(self) -> None:
        """Resolve a direct relative base lexically against cwd."""
        self.assertEqual(
            await self.base_without_entry({"base": "../hmr base"}), Path.cwd().parent / "hmr base"
        )

    async def test_accepts_an_absolute_path_without_a_context_directory(self) -> None:
        """Preserve literal spaces, hash and percent characters in an absolute base."""
        base = self.base / "base with spaces # %"
        self.assertEqual(await self.base_without_entry({"base": str(base)}), base)

    async def test_resolves_a_relative_base_against_the_context(self) -> None:
        """The configuration tree directory takes precedence over the process cwd."""
        self.assertEqual(await self.base_of_entry({"base": "./plugins"}), self.base / "plugins")

    async def test_defaults_to_the_context_directory(self) -> None:
        """An HMR entry defaults to its own tree's base directory."""
        self.assertEqual(await self.base_of_entry({}), self.base)

    async def test_watches_files_in_base(self) -> None:
        """Actual filesystem events outside the configured filter never reload a plugin."""
        watched = self.write("src/watched.rs", plugin("watched", "v1"))
        ignored = self.write("other/ignored.rs", plugin("ignored", "v1"))
        loader = await self.setup_app(
            {"enabled": True, "debounce": 100, "files": ["src", "!**/.*"]}
        )
        self.assertIn(f'watching ["src", "!**/.*"] in {self.base}', self.logs())
        await self.load(loader, watched, ignored)
        self.write("other/ignored.rs", plugin("ignored", "v2"))
        self.write("src/watched.rs", plugin("watched", "v2"))
        await self.eventually(lambda: self.query("watched") == "v2")
        await asyncio.sleep(0.3)
        self.assertEqual(self.query("ignored"), "v1")

    async def test_watches_only_when_enabled_with_files(self) -> None:
        """Disabled and empty-filter watches never start a native subscription."""
        configs: list[JsonValue] = [{}, {"enabled": True, "files": []}]
        for watch in configs:
            await self.setup_app(watch)
            self.assertFalse(any(log.startswith("watching") for log in self.logs()))
            await self.root.fiber().dispose()
        await self.setup_app()
        self.assertTrue(any(log.startswith("watching") for log in self.logs()))

    async def test_reconciles_the_changes_of_one_debounce_window_as_a_batch(self) -> None:
        """Two actual changes in one quiet window produce one transaction."""
        a = self.write("a.rs", plugin("a", "v1"))
        b = self.write("b.rs", plugin("b", "v1"))
        loader = await self.setup_app()
        await self.load(loader, a, b)
        self.write("a.rs", plugin("a", "v2"))
        self.write("b.rs", plugin("b", "v2"))
        await self.eventually(lambda: self.query("a") == "v2" and self.query("b") == "v2")
        await asyncio.sleep(0.3)
        self.assertEqual(sum(log.startswith("reconcile ") for log in self.logs()), 1)

    async def test_zero_debounce_consumes_already_queued_changes(self) -> None:
        """A zero timeout still polls queued paths before flushing, as Tokio does."""
        a = self.write("a.rs", plugin("a", "v1"))
        b = self.write("b.rs", plugin("b", "v1"))
        loader = await self.setup_app({})
        await self.load(loader, a, b)
        self.write("a.rs", plugin("a", "v2"))
        self.write("b.rs", plugin("b", "v2"))
        changes: asyncio.Queue[Path | None] = asyncio.Queue()
        changes.put_nowait(a)
        changes.put_nowait(b)
        verbose = self.root.intercept("logger", {"level": 3})
        task = asyncio.create_task(batches(HmrExt(verbose.get(Hmr)), self.base, changes, 0))
        try:
            await self.eventually(lambda: self.query("a") == "v2" and self.query("b") == "v2")
            self.assertEqual(self.logs().count("reconcile 2 changed files"), 1)
        finally:
            changes.put_nowait(None)
            await task

    async def test_retries_the_changes_of_a_failed_batch_with_the_next_one(self) -> None:
        """Repairing only the plugin also retries its previously failed dependency change."""
        dep = self.write("dep.rs", value("v1"))
        path = self.write("retry.rs", dependent("retry", str(dep)))
        loader = await self.setup_app()
        await self.load(loader, path)
        self.assertEqual(self.query("retry"), "v1")
        self.write("dep.rs", value("v2"))
        self.write("retry.rs", throws("retry is broken"))
        await self.eventually(lambda: any("retry is broken" in log for log in self.logs()))
        self.assertEqual(self.query("retry"), "v1")
        self.write("retry.rs", dependent("retry", str(dep)))
        await self.eventually(lambda: self.query("retry") == "v2")

    async def test_only_reports_a_change_to_a_host_module(self) -> None:
        """A host file change is reported relatively while the running host remains unchanged."""
        path = self.write("host.rs", plugin("host", "v1"))
        loader = await self.setup_app()
        await self.load(loader, path)
        self.graph.mark_host([str(path)])
        self.write("host.rs", plugin("host", "v2"))
        await self.eventually(lambda: "skipped host modules:\n- host.rs" in self.logs())
        self.assertEqual(self.query("host"), "v1")
        self.assertEqual(self.root.get(Hmr).arc().base_dir(), self.base)

    async def test_retries_the_changes_of_a_failed_batch_with_a_batch_queued_behind_it(
        self,
    ) -> None:
        """A real file repair queued during a blocked import retries the whole failed unit."""
        dep = self.write("dep.rs", value("v1"))
        path = self.write("queued.rs", dependent("queued", str(dep)))
        loader = await self.setup_app()
        await self.load(loader, path)
        entered, release = threading.Event(), threading.Event()

        def evaluate(imports: Imports) -> Exports:
            """Hold the real synchronous evaluator until the repair file has been written."""
            entered.set()
            if not release.wait(5):
                raise TimeoutError("repair did not release import")
            return Exports.value("not a plugin")

        def repair() -> None:
            """Write a repair from another thread while the import blocks the event loop."""
            try:
                if entered.wait(5):
                    self.write("queued.rs", dependent("queued", str(dep)))
                    time.sleep(0.3)
            finally:
                release.set()

        thread = threading.Thread(target=repair)
        thread.start()
        try:
            self.write("dep.rs", value("v2"))
            self.write("queued.rs", ModuleCode.new([str(dep)], evaluate))
            await self.eventually(lambda: self.query("queued") == "v2")
            self.assertTrue(any(log.startswith("invalid plugin at") for log in self.logs()))
        finally:
            release.set()
            await asyncio.to_thread(thread.join)
