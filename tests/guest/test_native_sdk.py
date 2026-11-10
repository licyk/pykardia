"""Run the same Python guest source through its compiled C ABI exports."""

import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from typing import ClassVar

from pykardia import Context
from pykardia.guest.export import export_dylib, export_process
from pykardia.hmr import DylibSource, ModuleGraph
from tests.guest import test_process_sdk


class NativeSdkTests(test_process_sdk.ProcessSdkTests):
    """Apply the shared behavioral assertions to actual CFFI embedding libraries."""

    build: ClassVar[tempfile.TemporaryDirectory[str]]
    library: ClassVar[Path]
    kind = "dylib"

    @classmethod
    def setUpClass(cls) -> None:
        """Compile once outside asyncio's test loop."""
        cls.build = tempfile.TemporaryDirectory()
        cls.library = Path(cls.build.name) / "counter.so"
        export_dylib(Path(__file__).with_name("counter.py"), "Counter", cls.library)

    @classmethod
    def tearDownClass(cls) -> None:
        """Remove the artifact after the host has retained its library mapping."""
        cls.build.cleanup()

    async def asyncSetUp(self) -> None:
        """Create fresh roots and graph while reusing the compiled source artifact."""
        self.temp = tempfile.TemporaryDirectory()
        self.path = self.library
        self.graph = ModuleGraph(test_process_sdk.WithData(DylibSource()))
        self.root = Context()
        self.second = Context()

    def build_guest(self, guest: str, output: Path) -> None:
        """Compile another guest class into a fresh native library."""
        export_dylib(Path(__file__).with_name("counter.py"), guest, output)


class ExportBundleTests(unittest.TestCase):
    """Verify packaged source runs without an installed pykardia or typing-extensions."""

    def test_process_archive_runs_without_site_packages_or_original_source(self) -> None:
        """A separate isolated interpreter imports the SDK and guest from the zipapp alone."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "counter"
            source = Path(directory) / "source.py"
            helper = Path(directory) / "helper.py"
            helper.write_text('LABEL = " bundled helper 你好"\n')
            text = Path(__file__).with_name("counter.py").read_text()
            source.write_text(
                "from helper import LABEL\n"
                + text.replace(
                    'Meta.plugin("python-counter")', 'Meta.plugin("python-counter" + LABEL)'
                )
            )
            export_process(source, "Counter", path, module_root=Path(directory))
            source.unlink()
            helper.unlink()
            with zipfile.ZipFile(path) as bundle:
                self.assertIn(b"MIT License", bundle.read("LICENSES/pykardia-guest-LICENSE-MIT"))
                self.assertIn(
                    b"Apache License", bundle.read("LICENSES/pykardia-guest-LICENSE-APACHE")
                )
                self.assertNotIn("LICENSES/pykardia-LICENSE", bundle.namelist())
                self.assertIn(
                    b"Python Software Foundation", bundle.read("LICENSES/typing_extensions-LICENSE")
                )
            result = subprocess.run(
                [sys.executable, "-I", "-S", str(path)],
                input=b'{"export":"meta"}\n{"export":"dispose"}\n',
                capture_output=True,
                cwd=directory,
                env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
                check=True,
                timeout=10,
            )
        self.assertIn(b"python-counter", result.stdout)
        self.assertIn("bundled helper 你好".encode(), result.stdout)
        self.assertEqual(result.stdout.count(b'"done":0'), 2)
        self.assertEqual(result.stderr, b"")
