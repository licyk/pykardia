"""Compile the same typed Python guest into a bare-WASM module and exercise its ABI."""

import os
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

import wasmtime

from pykardia import Context
from pykardia.guest.wasm_build import export_wasm
from pykardia.hmr import ModuleGraph, WasmSource
from tests.guest import test_process_sdk


@unittest.skipUnless(
    all(
        os.environ.get(name)
        for name in ("PYKARDIA_PYTHON_SOURCE", "PYKARDIA_PYTHON_BUILD", "PYKARDIA_WASI_SDK")
    ),
    "requires the CPython WASI build and WASI SDK used by the binary export acceptance job",
)
class WasmSdkTests(test_process_sdk.ProcessSdkTests):
    """Run all SDK behavior assertions against a freshly linked production artifact."""

    build: ClassVar[tempfile.TemporaryDirectory[str]]
    binary: ClassVar[Path]
    kind = "wasm"

    @classmethod
    def setUpClass(cls) -> None:
        """Link the actual SDK and the shared guest fixture once for this test class."""
        cls.build = tempfile.TemporaryDirectory()
        cls.binary = Path(cls.build.name) / "counter.wasm"
        export_wasm(
            Path(__file__).with_name("counter.py"),
            "Counter",
            cls.binary,
            Path(os.environ["PYKARDIA_PYTHON_SOURCE"]),
            Path(os.environ["PYKARDIA_PYTHON_BUILD"]),
            Path(os.environ["PYKARDIA_WASI_SDK"]),
        )

    @classmethod
    def tearDownClass(cls) -> None:
        """Remove the generated module after all stores have been disposed."""
        cls.build.cleanup()

    async def asyncSetUp(self) -> None:
        """Create new stores through the real WASM module source for each scenario."""
        self.temp = tempfile.TemporaryDirectory()
        self.path = self.binary
        self.graph = ModuleGraph(test_process_sdk.WithData(WasmSource()))
        self.root = Context()
        self.second = Context()

    async def test_exports_both_name_sets_and_imports_cordis(self) -> None:
        """Every export has a kardia_* twin of the same type; imports use cordis only."""
        compiled = wasmtime.Module.from_file(wasmtime.Engine(), str(self.binary))
        exports = {
            item.name: (
                [str(kind) for kind in item.type.params],
                [str(kind) for kind in item.type.results],
            )
            for item in compiled.exports
            if isinstance(item.type, wasmtime.FuncType)
        }
        for name in ("meta", "apply", "call", "dispose"):
            self.assertIn(f"cordis_{name}", exports)
            self.assertEqual(exports.get(f"kardia_{name}"), exports[f"cordis_{name}"])
        self.assertEqual({item.module for item in compiled.imports}, {"cordis"})

    async def test_wasm_packages_license_notices(self) -> None:
        """The executable WASM artifact retains the bundled runtime and SDK notices."""
        data = self.binary.read_bytes()
        self.assertIn(b"pykardia.licenses", data)
        for name in (
            "CPython-LICENSE",
            "CPython-third-party",
            "pykardia-guest-LICENSE-MIT",
            "pykardia-guest-LICENSE-APACHE",
            "pykardia-NOTICE",
            "typing_extensions-LICENSE",
        ):
            self.assertIn(f"LICENSES/{name}".encode(), data)

    def build_guest(self, guest: str, output: Path) -> None:
        """Link another guest class with the same configured CPython WASI toolchain."""
        export_wasm(
            Path(__file__).with_name("counter.py"),
            guest,
            output,
            Path(os.environ["PYKARDIA_PYTHON_SOURCE"]),
            Path(os.environ["PYKARDIA_PYTHON_BUILD"]),
            Path(os.environ["PYKARDIA_WASI_SDK"]),
        )
