"""Exact cross-language expression values, error messages and located offsets."""

import json
import unittest
from dataclasses import dataclass
from pathlib import Path

from pykardia import Context, JsonValue, decode
from pykardia.loader.expr import ExprError, interpolate


@dataclass
class ExprInput:
    """Checked boundary input to the thin Rust/Python observation drivers."""

    services: dict[str, JsonValue]
    expressions: list[str]


class ExprParityTests(unittest.IsolatedAsyncioTestCase):
    """Compare actual evaluators without reproducing expression logic in the tests."""

    async def test_rust_reference(self) -> None:
        """Match fixed expression records including Unicode, numeric edges and syntax errors."""
        directory = Path(__file__).parent / "expr"
        inputs = decode(json.loads((directory / "input.json").read_text()), ExprInput)
        expected = json.loads((directory / "reference.json").read_text())
        ctx = Context()
        handles = [ctx.provide_as(name, value) for name, value in inputs.services.items()]
        try:
            for source, reference in zip(inputs.expressions, expected, strict=True):
                with self.subTest(source=source):
                    observed: dict[str, JsonValue]
                    try:
                        observed = {"value": interpolate(ctx, {"config": [{"__jsExpr": source}]})}
                    except ExprError as error:
                        observed = {
                            "error": str(error),
                            "offset": error.offset,
                            "path": [str(part) for part in error.path],
                        }
                    self.assertEqual(observed, reference)
        finally:
            for handle in reversed(handles):
                await handle.dispose()
