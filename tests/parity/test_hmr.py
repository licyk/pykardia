"""Compare real transactional reconciliation against the pinned Rust runtime."""

import json
import unittest
from pathlib import Path

from tests.hmr.common import APP
from tests.parity.hmr.probe import run


class HmrParityTests(unittest.IsolatedAsyncioTestCase):
    """Compare unit failures, per-entry identity and settled membership, config and cache values."""

    async def test_matches_pinned_rust_trace(self) -> None:
        """Require the complete sequence of stage observations to match actual Rust output."""
        reference = Path(__file__).parent / "hmr" / "reference.json"
        self.assertEqual(await run(), json.loads(reference.read_text().replace("/app/", f"{APP}/")))
