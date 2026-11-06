"""Compare public loader behavior against actual output of the pinned Rust runtime."""

import json
import unittest
from pathlib import Path

from tests.parity.loader.probe import run


class LoaderParityTests(unittest.IsolatedAsyncioTestCase):
    """Compare ordered lifecycle observations and unnormalized source snapshots."""

    async def test_matches_pinned_rust_trace(self) -> None:
        """Require every recorded value and identity relation to match the Rust result."""
        reference = Path(__file__).parent / "loader" / "reference.json"
        self.assertEqual(await run(), json.loads(reference.read_text()))
