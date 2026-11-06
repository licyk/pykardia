"""Context derivation and ownership tests against the public package entry point."""

import unittest
from dataclasses import dataclass

from pykardia import Context, FiberId, FiberState, Overlay, RealmId, merge_shallow


@dataclass
class Metadata:
    """A defaultable metadata target for shallow merge and malformed-value fallback."""

    enabled: bool = False
    label: str = "default"


class ContextTests(unittest.TestCase):
    """Use contexts without an event loop for synchronous derivation and effects."""

    def test_root_and_derived_identity(self) -> None:
        """Derivation changes scope identity while retaining the root and resource owner."""
        root = Context.new()
        derived = root.extend()
        self.assertTrue(root.same_root(derived))
        self.assertFalse(root.ptr_eq(derived))
        self.assertTrue(derived.root().ptr_eq(root))
        self.assertTrue(derived.is_root())
        self.assertEqual(derived.fiber_id(), FiberId.ROOT)
        self.assertTrue(derived.fiber().context().ptr_eq(root))
        self.assertEqual(root.fiber().parent(), root.fiber())
        self.assertEqual(root.fiber().state(), FiberState.Active)
        self.assertFalse(root.same_root(Context()))

    def test_shared_realms_reparent_and_parent_overlay(self) -> None:
        """Reuse explicit realms and move existing descendants with their parent node."""
        root = Context()
        overlay = Overlay.tagged(42)
        parent = root.extend_with(overlay)
        child = parent.intercept("db", {})
        self.assertIs(child.parent_overlay(), overlay)
        self.assertIs(parent.overlay(), overlay)
        self.assertIsNone(child.overlay())
        self.assertEqual(child.tag(), 42)
        shared = RealmId.new()
        target = root.isolate_in("db", shared)
        moved = parent.reparent(target)
        self.assertTrue(moved.ptr_eq(parent))
        self.assertEqual(child.realm("db"), shared)
        self.assertTrue(child.has_isolation("db"))
        self.assertTrue(child.is_within(target))
        self.assertIsNone(root.realm("db"))
        self.assertNotEqual(root.isolate("db").realm("db"), shared)

    def test_shallow_merge_and_metadata_default(self) -> None:
        """Later objects win by field; malformed typed metadata defaults as a whole."""
        root = Context()
        context = root.intercept("meta", {"enabled": True}).intercept("meta", {"label": "inner"})
        self.assertEqual(context.intercept_meta("meta", Metadata), Metadata(True, "inner"))
        malformed = context.intercept("meta", {"label": 5})
        self.assertEqual(malformed.intercept_meta("meta", Metadata), Metadata())
        self.assertEqual(
            merge_shallow([{"nested": {"a": 1}}, None, [1], {"nested": {"b": 2}}]),
            {"nested": {"b": 2}},
        )

    def test_sync_effect_works_without_running_loop(self) -> None:
        """Purely synchronous setup and disposal need no coroutine driver."""
        root = Context()
        sequence: list[int] = []
        handle = root.on_dispose(lambda: sequence.append(1))
        self.assertEqual(root.fiber().effect_count(), 1)
        self.assertTrue(handle.dispose().is_ready())
        self.assertEqual(sequence, [1])
        self.assertEqual(root.fiber().effect_count(), 0)
