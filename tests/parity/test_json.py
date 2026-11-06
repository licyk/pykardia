"""Compare JSON file parsing and formatting with the original serde_json boundary."""

import json
import unittest
from pathlib import Path

from pykardia.core.json import parse
from pykardia.include.document import pretty_json
from pykardia.include.yaml import ParseError


class JsonParityTests(unittest.TestCase):
    """Preserve JSON values, exact pretty output and byte-based diagnostic coordinates."""

    def test_matches_pinned_rust_json(self) -> None:
        """Check strict numbers, surrogate pairs, truncation, delimiters and Unicode columns."""
        base = Path(__file__).parent / "json"
        cases = json.loads((base / "input.json").read_text())
        reference = json.loads((base / "reference.json").read_text())
        for source, expected in zip(cases, reference, strict=True):
            with self.subTest(source=source):
                try:
                    value = parse(source)
                except ParseError as error:
                    actual = {
                        "error": error.message,
                        "position": [error.position.line, error.position.column],
                    }
                else:
                    actual = {"value": value, "pretty": pretty_json(value)}
                self.assertEqual(actual, expected)
