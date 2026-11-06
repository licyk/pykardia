"""Compare actual malformed-guest observations against the original Rust WASM host."""

import json
import os
import tempfile
import unittest
from pathlib import Path

import wasmtime

from pykardia.hmr import ModuleGraph, WasmSource
from pykardia.hmr.guest import module
from pykardia.hmr.wasm import WasmCode
from tests.hmr.wat import wat
from tests.parity.python_guest.boundaries import fixtures


class GuestBoundaryTests(unittest.TestCase):
    """Keep stable protocol diagnostics exact and identify Python's callback traceback boundary."""

    def test_relative_dependencies_drop_excess_parent_segments(self) -> None:
        """Read real WASM metadata with the source's lexical PathBuf::pop behavior."""
        engine = wasmtime.Engine()
        code = WasmCode(
            engine,
            wasmtime.Module(engine, wat({"deps": ["../../../dep", "..", "//dir//x"]}, [], "")),
        )
        # Windows treats the double leading separator as a UNC path.
        absolute = Path("//dir//x") if os.name == "nt" else Path("/dir/x")
        self.assertEqual(module("file.wat", code).deps, ["dep", "", str(absolute)])

    def test_rust_guest_metadata_and_abi_observations(self) -> None:
        """Load all real WAT artifacts through the production host and compare the Rust trace."""
        reference = dict(
            json.loads(
                Path(__file__).with_name("python_guest").joinpath("boundaries.json").read_text()
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            for name, source in fixtures().items():
                with self.subTest(name=name):
                    path = Path(directory) / f"{name}.wat"
                    path.write_text(source)
                    try:
                        ModuleGraph(WasmSource()).import_(str(path))
                        actual = "ok"
                    except Exception as error:
                        actual = str(error).replace(str(path), "<module>")
                    if name in {"no_memory", "bounds"}:
                        self.assertTrue(
                            reference[name].startswith("error while executing at wasm backtrace:")
                        )
                        self.assertEqual(actual, reference[name].rsplit(": ", 1)[1])
                    else:
                        self.assertEqual(actual, reference[name])
