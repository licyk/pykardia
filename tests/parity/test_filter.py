"""Compare the real path filter against the pinned Rust/globset implementation."""

import json
import unittest
from pathlib import Path

from pykardia.fs import admits, compile


class FilterParityTests(unittest.TestCase):
    """Exercise fixed patterns against files, directories and traversal admission."""

    def test_matches_pinned_rust_filter(self) -> None:
        """Keep byte wildcard semantics and exact parser errors from the source toolchain."""
        base = Path(__file__).parent / "fs"
        cases = json.loads((base / "input.json").read_text())
        expected = json.loads((base / "reference.json").read_text())
        for case, reference in zip(cases, expected, strict=True):
            with self.subTest(patterns=case["patterns"]):
                try:
                    keep = compile(case["patterns"])
                except ValueError as error:
                    self.assertEqual({"error": str(error)}, reference)
                else:
                    result = [
                        [keep(path, directory), admits(keep, path, directory)]
                        for path, directory in case["paths"]
                    ]
                    self.assertEqual({"results": result}, reference)
