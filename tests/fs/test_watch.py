"""Real filesystem and listener lifetime acceptance for caller-owned watches."""

import asyncio
import os
import sys
import tempfile
import threading
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

import async_timeout

from pykardia import Context, JsonValue, Level, PluginDef
from pykardia.fs import Filter, Fs, FsExt, FsPlugin, Subscription, Watcher, WatchOptions


async def eventually(check: Callable[[], bool]) -> None:
    """Wait a bounded interval for actual native event delivery."""
    async with async_timeout.timeout(5):
        while not check():
            await asyncio.sleep(0.01)


class WatchTests(unittest.IsolatedAsyncioTestCase):
    """Create independent native watches and verify cleanup before deleting their files."""

    async def asyncSetUp(self) -> None:
        """Install the real fs service and a consumer fiber in a fresh temporary directory."""
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = Context()
        await self.root.plugin(FsPlugin, None).settled()

        async def apply(ctx: Context, config: JsonValue) -> None:
            """Provide the actual resource-owning consumer context."""

        self.fiber = self.root.plugin_def(PluginDef.new(apply).inject(["fs"]))
        await self.fiber.settled()
        self.fs = FsExt(self.fiber.context().get(Fs))
        self.watchers: list[Watcher] = []

    async def asyncTearDown(self) -> None:
        """Drain every inverse and confirm no observer thread survives its owner."""
        await self.root.fiber().dispose()
        for watcher in self.watchers:
            self.assertFalse(watcher._observer.is_alive())
        self.temp.cleanup()

    def file(self, name: str) -> Path:
        """Create an actual file and any required parent directories."""
        path = self.base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("initial")
        return path

    def watch(self, path: str | Path, filter: Filter | None = None) -> Watcher:
        """Start a watch and retain it for thread-leak verification."""
        watcher = self.fs.watch(path, WatchOptions(filter))
        self.watchers.append(watcher)
        return watcher

    async def test_file_atomic_replacement_and_filter_independence(self) -> None:
        """A file watch survives repeated rename, ignores siblings and bypasses its filter."""
        path, sibling = self.file(".parent/.file"), self.file(".parent/sibling")
        watcher = self.watch(path, [])
        seen: list[Path] = []
        watcher.on_change(seen.append)
        path.write_text("changed")
        sibling.write_text("ignored")
        await eventually(lambda: path in seen)
        for value in ("v1", "v2"):
            seen.clear()
            temporary = self.file(".parent/tmp")
            temporary.write_text(value)
            temporary.replace(path)
            await eventually(lambda: path in seen)
        seen.clear()
        path.write_text("still watched")
        await eventually(lambda: path in seen)
        self.assertEqual(set(seen), {path})
        self.assertEqual(watcher.context(), self.fiber.context())
        self.assertEqual(self.fiber.effect_count(), 1)

    async def test_recursive_filter_and_caller_disposal(self) -> None:
        """Recursive changes use the selected directory filter and end with the caller."""
        kept, ignored = self.file("src/a.rs"), self.file("notes.txt")
        watcher = self.watch(self.base, ["src/**"])
        seen: list[Path] = []
        watcher.on_change(seen.append)
        kept.write_text("changed")
        ignored.write_text("ignored")
        await eventually(lambda: kept in seen)
        self.assertNotIn(ignored, seen)
        await self.fiber.dispose()
        seen.clear()
        kept.write_text("after")
        await asyncio.sleep(0.05)
        self.assertEqual(seen, [])
        self.assertFalse(watcher._observer.is_alive())

    async def test_relative_path_and_directory_trailing_slash(self) -> None:
        """Resolve relative paths lexically against cwd and accept a trailing separator."""
        path = self.file("nested/file.txt")
        seen: list[Path] = []
        relative = os.path.relpath(path, Path.cwd())
        self.watch(relative).on_change(seen.append)
        from_dir: list[Path] = []
        self.watch(str(path.parent) + "/", lambda name, directory: name == "file.txt").on_change(
            from_dir.append
        )
        path.write_text("changed")
        await eventually(lambda: bool(seen) and bool(from_dir))
        self.assertEqual(set(seen), {path})
        self.assertEqual(set(from_dir), {path})

    async def test_filter_types_and_each_watch_independent(self) -> None:
        """Predicates receive relative ancestor types without filtering the watched root."""
        kept, hidden = self.file(".root/nested/kept.txt"), self.file(".root/.claimed.yml")
        calls: list[tuple[str, bool]] = []

        def predicate(path: str, directory: bool) -> bool:
            """Observe real filter boundaries and reject hidden descendants."""
            calls.append((path, directory))
            return not path.startswith(".")

        directory_seen: list[Path] = []
        file_seen: list[Path] = []
        self.watch(self.base / ".root", predicate).on_change(directory_seen.append)
        self.watch(hidden).on_change(file_seen.append)
        kept.write_text("changed")
        hidden.write_text("claimed")
        await eventually(lambda: bool(directory_seen) and bool(file_seen))
        self.assertEqual(set(directory_seen), {kept})
        self.assertEqual(set(file_seen), {hidden})
        self.assertIn(("nested", True), calls)
        self.assertIn(("nested/kept.txt", False), calls)
        self.assertTrue(
            all(path and not path.startswith("/") and "\\" not in path for path, _ in calls)
        )

    async def test_symlink_directory_existing_and_new(self) -> None:
        """Existing and newly introduced directory links report paths through the link."""
        for existing in (True, False):
            target = self.file(f"target-{existing}/file.ts")
            link = self.file(f"root-{existing}/linked")
            if existing:
                link.unlink()
                link.symlink_to(target.parent, target_is_directory=True)
            watcher = self.watch(link.parent, ["linked/**/*.ts"])
            seen: list[Path] = []
            watcher.on_change(seen.append)
            if not existing:
                link.unlink()
                link.symlink_to(target.parent, target_is_directory=True)
                await eventually(lambda link=link, watcher=watcher: link in watcher._links)
            target.write_text("changed")
            await eventually(lambda seen=seen: bool(seen))
            self.assertEqual(set(seen), {link / "file.ts"})

    async def test_rejects_outside_filter_before_collecting_effect(self) -> None:
        """Invalid external paths fail at the boundary without starting a native watcher."""
        for pattern in ("../outside", "!../outside", "a/../../outside", "/absolute"):
            with self.assertRaisesRegex(ValueError, "leads outside"):
                self.fs.watch(self.base, WatchOptions([pattern]))
        self.assertEqual(self.fiber.effect_count(), 0)

    async def test_shared_path_disposal_and_runtime_thread(self) -> None:
        """Each watch owns its callbacks and executes them on the runtime thread."""
        path = self.file("shared.txt")
        first, second = self.watch(path), self.watch(path)
        calls: list[str] = []
        threads: list[int] = []
        first.on_change(lambda path: calls.append("a"))
        second.on_change(lambda path: calls.append("b"))
        second.on_change(lambda path: threads.append(threading.get_ident()))
        path.write_text("first")
        await eventually(lambda: "a" in calls and "b" in calls)
        await first.dispose()
        calls.clear()
        path.write_text("second")
        await eventually(lambda: "b" in calls)
        self.assertNotIn("a", calls)
        self.assertEqual(set(threads), {threading.get_ident()})

    async def test_ready_async_nested_removed_and_readded(self) -> None:
        """Ready registration queues unique IDs, including registrations during delivery."""
        watcher = self.watch(self.file("ready"))
        calls: list[str] = []
        removal: list[Subscription] = []

        def replace() -> None:
            """Remove a queued callback and add a new registration during the dispatch."""
            removal[0].remove()
            watcher.on_ready(lambda: calls.append("readded"))

        watcher.on_ready(replace)
        removal.append(watcher.on_ready(lambda: calls.append("removed")))
        watcher.on_ready(lambda: calls.append("cancelled")).remove()
        watcher.on_ready(lambda: calls.append("ordinary"))
        self.assertEqual(calls, [])
        await eventually(lambda: len(calls) == 2)
        await asyncio.sleep(0.02)
        self.assertEqual(calls, ["ordinary", "readded"])
        watcher.on_ready(lambda: calls.append("late"))
        self.assertEqual(len(calls), 2)
        await eventually(lambda: len(calls) == 3)

    async def test_error_delivery_warning_and_removal_during_dispatch(self) -> None:
        """Only native errors are injected; all subscriptions and dispatch remain real."""
        watcher = self.watch(self.base)
        watcher._post(watcher._error_signal, OSError("EPERM"))
        await eventually(lambda: any("EPERM" in m.text for m in self.root.log_buffer()))
        before = len([m for m in self.root.log_buffer() if m.level == Level.Warn])
        removed: list[Subscription] = []
        calls: list[str] = []
        watcher.on_error(lambda error: removed[0].remove())
        removed.append(watcher.on_error(lambda error: calls.append("removed")))
        watcher.on_error(lambda error: calls.append(str(error)))
        watcher._post(watcher._error_signal, OSError("handled"))
        await eventually(lambda: bool(calls))
        self.assertEqual(calls, ["handled"])
        self.assertEqual(len([m for m in self.root.log_buffer() if m.level == Level.Warn]), before)

    async def test_delete_does_not_become_change(self) -> None:
        """File deletion alone never triggers the source create/modify change channel."""
        path = self.file("removed.txt")
        seen: list[Path] = []
        self.watch(path).on_change(seen.append)
        path.unlink()
        await asyncio.sleep(0.05)
        self.assertEqual(seen, [])

    @unittest.skipUnless(sys.platform == "linux", "requires the Linux inotify backend")
    async def test_native_reader_error_enters_error_channel(self) -> None:
        """Inject only the otherwise unprovokable OS read failure into a real observer."""
        from watchdog.observers.inotify_c import Inotify

        errors: list[str] = []
        with patch.object(Inotify, "read_events", side_effect=OSError("native read failed")):
            watcher = self.watch(self.base)
            watcher.on_error(lambda error: errors.append(str(error)))
            await eventually(lambda: bool(errors))
        self.assertEqual(errors, ["native read failed"])
        await watcher.dispose()

    async def test_failed_watch_setup_does_not_leave_an_effect(self) -> None:
        """A missing parent fails before returning a watcher and unwinds its native setup."""
        with self.assertRaises(FileNotFoundError):
            self.fs.watch(self.base / "missing" / "file.txt")
        await asyncio.sleep(0)
        self.assertEqual(self.fiber.effect_count(), 0)
