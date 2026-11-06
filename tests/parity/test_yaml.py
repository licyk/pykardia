"""Compare parser values, dumps and source positions against actual pinned Rust results."""

import json
import unittest
from pathlib import Path

from pykardia.core.value import JsonValue
from pykardia.include.yaml import ParseError, Position, dump, parse


def position(value: Position | None) -> JsonValue:
    """Encode a position using the Rust fixture's pair-or-null observation shape."""
    return [value.line, value.column] if value is not None else None


class YamlParityTests(unittest.TestCase):
    """Keep parser and formatter output exact across the fixed source cases."""

    def test_matches_pinned_rust_yaml(self) -> None:
        """Compare all source values, dumps, diagnostic positions and expression offsets."""
        base = Path(__file__).parent / "yaml"
        cases = json.loads((base / "input.json").read_text())
        reference = json.loads((base / "reference.json").read_text())
        for case, expected in zip(cases, reference, strict=True):
            with self.subTest(source=case["source"]):
                try:
                    value, syntax = parse(case["source"])
                except ParseError as error:
                    result = {"error": error.message, "position": position(error.position)}
                else:
                    result = {
                        "value": value,
                        "dump": dump(value),
                        "positions": [position(syntax.locate(path)) for path in case["paths"]],
                        "expressions": [
                            position(syntax.locate_expr(path, offset))
                            for path, offset in case["exprs"]
                        ],
                    }
                self.assertEqual(result, expected)
