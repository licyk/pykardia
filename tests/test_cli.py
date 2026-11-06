"""Exercise the installed CLI flow through real subprocesses, files, guests and signals."""

import asyncio
import json
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import async_timeout

import pykardia
from pykardia.cli import GuestFiles, Plugins
from pykardia.guest.export import export_dylib
from pykardia.hmr import ModuleGraph
from tests.hmr.wat import greeter


class FacadeTests(unittest.IsolatedAsyncioTestCase):
    """Verify unified public modules and the built-in-first resolver."""

    async def test_catalog_and_original_aliases_share_definitions(self) -> None:
        """Every built-in alias snapshots its real definition without an implicit HMR graph."""
        catalog = pykardia.catalog()
        aliases = {
            "group": "group",
            "timer": "timer",
            "logger-console": "logger-console",
            "fs": "fs-node",
            "include": "include",
        }
        for name, alias in aliases.items():
            self.assertIs(catalog.get(name), catalog.get(f"@cordisjs/plugin-{alias}"))
            self.assertIsNotNone(catalog.get(name))
        self.assertIsNone(catalog.get("hmr"))
        resolver = Plugins(catalog, ModuleGraph(GuestFiles()), Path("/does-not-exist"))
        self.assertIs(await resolver.resolve("timer"), catalog.get("timer"))
        from pykardia import prelude

        self.assertIs(prelude.Context, pykardia.Context)
        self.assertIs(prelude.Loader, pykardia.loader.Loader)
        self.assertIs(prelude.Timer, pykardia.timer.Timer)
        for name in ("fs", "guest", "hmr", "include", "loader", "logger_console", "timer"):
            self.assertEqual(getattr(pykardia, name).__name__, f"pykardia.{name}")


class CliTests(unittest.IsolatedAsyncioTestCase):
    """Run the actual command's default/explicit path, error exits and live reload lifecycle."""

    async def asyncSetUp(self) -> None:
        """Create an isolated working directory without mutating global cwd or signal handlers."""
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.process: asyncio.subprocess.Process | None = None
        self.lines: list[str] = []

    async def asyncTearDown(self) -> None:
        """Reap the CLI even when an assertion fails, then remove its configuration files."""
        if self.process is not None and self.process.returncode is None:
            self.process.kill()
            await self.process.communicate()
        self.temp.cleanup()

    async def launch(self, *args: str) -> asyncio.subprocess.Process:
        """Start the module entry point with real pipes and the test directory as cwd."""
        self.process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "pykardia",
            *args,
            cwd=self.base,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
        )
        return self.process

    async def until(self, text: str) -> None:
        """Read actual console records until the requested runtime observation arrives."""
        assert self.process is not None and self.process.stdout is not None
        async with async_timeout.timeout(8):
            while True:
                line = (await self.process.stdout.readline()).decode()
                self.lines.append(line)
                if text in line:
                    return
                if not line:
                    self.fail(f"CLI exited before {text}: {self.lines}")

    async def test_default_file_live_config_hmr_and_sigint_cleanup(self) -> None:
        """Real WAT replacement and config edits reconcile before SIGINT drains both watches."""
        guest = self.base / "guest.wat"
        guest.write_text(greeter("v1"))
        config = self.base / "pykardia.yml"
        entries: list[dict[str, pykardia.JsonValue]] = [
            {"id": "hmr", "name": "hmr", "config": {"watch": {"enabled": True, "debounce": 25}}},
            {"id": "guest", "name": "guest.wat", "disabled": False},
        ]
        config.write_text(json.dumps(entries))
        process = await self.launch()
        await self.until("greeter v1 applied")
        # Guest apply logs can precede completion of the parallel HMR watch startup.
        await asyncio.sleep(0.15)
        guest.write_text(greeter("v2"))
        await self.until("greeter v2 applied")
        entries[1]["disabled"] = True
        config.write_text(json.dumps(entries))
        await self.until("greeter v2 disposed")
        entries[1]["disabled"] = False
        config.write_text(json.dumps(entries))
        await self.until("greeter v2 applied")
        process.send_signal(signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGINT)
        stdout, stderr = await asyncio.wait_for(process.communicate(), 8)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(stderr, b"")
        self.assertEqual(json.loads(config.read_text()), entries)

    async def test_explicit_path_resolves_guests_from_config_directory(self) -> None:
        """A path containing spaces selects the file's directory instead of the CLI cwd."""
        directory = self.base / "config with spaces"
        directory.mkdir()
        (directory / "guest.wat").write_text(greeter("relative"))
        (directory / "app.yml").write_text('[{"name":"guest.wat"}]')
        process = await self.launch("config with spaces/app.yml")
        await self.until("greeter relative applied")
        await asyncio.sleep(0.15)
        process.send_signal(signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGINT)
        stdout, stderr = await asyncio.wait_for(process.communicate(), 8)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(stderr, b"")

    async def test_initial_include_error_returns_failure_and_logs_to_stdout(self) -> None:
        """Reject missing or malformed initial config before waiting for a signal."""
        for path, content in [("missing.yml", None), ("broken.yml", "[unclosed")]:
            if content is not None:
                (self.base / path).write_text(content)
            process = await self.launch(path)
            stdout, stderr = await asyncio.wait_for(process.communicate(), 8)
            self.assertEqual(process.returncode, 1)
            self.assertIn(b"[E] app", stdout)
            self.assertIn(path.encode(), stdout)
            self.assertEqual(stderr, b"")

    async def test_sigint_runs_native_guest_disposal(self) -> None:
        """Observe shutdown through a real guest-owned file after console removal."""
        guest = self.base / "cleanup.so"
        marker = self.base / "disposed.txt"
        export_dylib(Path(__file__).parent / "guest" / "cleanup.py", "Cleanup", guest)
        (self.base / "pykardia.yml").write_text(
            json.dumps([{"name": "cleanup.so", "config": {"path": str(marker)}}])
        )
        process = await self.launch()
        await self.until("cleanup ready")
        await asyncio.sleep(0.15)
        process.send_signal(signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGINT)
        _, stderr = await asyncio.wait_for(process.communicate(), 8)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(stderr, b"")
        self.assertEqual(marker.read_text(), "disposed 你好")
