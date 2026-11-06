"""Compare actual nested include synchronization with the pinned Rust runtime."""

import json
import unittest
from pathlib import Path

from tests.parity.include.probe import run


class IncludeParityTests(unittest.IsolatedAsyncioTestCase):
    """Require exact lifecycle ordering and full persisted text across owner changes."""

    async def test_matches_pinned_rust_trace(self) -> None:
        """Compare every record without sorting events or normalizing file contents."""
        reference = Path(__file__).parent / "include" / "reference.json"
        self.assertEqual(await run(), json.loads(reference.read_text()))
