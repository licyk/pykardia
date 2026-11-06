"""Compare Python lifecycle observations with output captured from the pinned Rust runtime."""

import json
import unittest
from pathlib import Path

from tests.parity.core_lifecycle.probe import run


class CoreLifecycleParityTests(unittest.IsolatedAsyncioTestCase):
    """Use Rust-generated observations without implementing another runtime in the test."""

    async def test_matches_pinned_rust_trace(self) -> None:
        """Compare the full ordered trace, identities, configurations and final resource counts."""
        reference = Path(__file__).parent / "core_lifecycle" / "reference.json"
        self.assertEqual(await run(), json.loads(reference.read_text()))
