"""Derivation and mutable-overlay semantics from core/src/scope.rs."""

import unittest

from pykardia.core.key import FiberId, Key, RealmId
from pykardia.core.scope import FiberScope, InterceptScope, IsolateScope, Overlay, Scope
from pykardia.core.value import JsonValue


class ScopeTests(unittest.TestCase):
    """Verify the real chain rather than cached copies of its lookup results."""

    def test_reparent_changes_existing_descendants(self) -> None:
        """Moving a middle node immediately changes descendant realms and fiber ancestry."""
        first = Scope.root()
        second = Scope.child(Scope.root(), FiberScope(FiberId(2)))
        realm = RealmId.new()
        isolated = Scope.child(second, IsolateScope(Key("db"), realm))
        middle = Scope.child(first, Overlay())
        descendant = Scope.child(middle, Overlay())
        self.assertTrue(descendant.is_within(first))
        self.assertIsNone(descendant.own_realm("db"))
        middle.set_parent(isolated)
        self.assertFalse(descendant.is_within(first))
        self.assertTrue(descendant.is_within(second))
        self.assertEqual(descendant.own_realm("db"), realm)
        self.assertEqual(descendant.fiber(), FiberId(2))
        self.assertTrue(descendant.is_within(descendant))

    def test_shared_overlay_and_copied_tables(self) -> None:
        """Shared overlays stay live while table snapshots cannot mutate them indirectly."""
        overlay = Overlay.tagged(0)
        one = Scope.child(Scope.root(), overlay)
        two = Scope.child(Scope.root(), overlay)
        realm = RealmId.new()
        table = {"db": realm}
        overlay.set_isolations(table)
        table.clear()
        overlay.isolations().clear()
        self.assertEqual(one.own_realm("db"), realm)
        self.assertEqual(two.own_realm("db"), realm)
        self.assertEqual(one.tag(), 0)
        overlay.set_isolations({})
        self.assertIsNone(one.own_realm("db"))
        self.assertIsNone(two.own_realm("db"))

    def test_intercepts_keep_outer_order_null_and_snapshot_ownership(self) -> None:
        """Metadata preserves override order and cannot be changed through returned JSON."""
        outer = Scope.child(Scope.root(), InterceptScope(Key("db"), {"outer": 1}))
        overlay = Overlay()
        middle = Scope.child(outer, overlay)
        inner = Scope.child(middle, InterceptScope(Key("db"), {"inner": 3}))
        source: dict[str, JsonValue] = {"db": {"middle": [2]}}
        overlay.set_intercepts(source)
        source.clear()
        metadata = inner.intercepts("db")
        self.assertEqual(metadata, [{"outer": 1}, {"middle": [2]}, {"inner": 3}])
        assert isinstance(metadata[1], dict)
        metadata[1]["middle"] = 99
        self.assertEqual(inner.intercepts("db")[1], {"middle": [2]})
        overlay.set_intercepts({"db": None})
        self.assertEqual(inner.intercepts("db"), [{"outer": 1}, None, {"inner": 3}])

    def test_nearest_isolation_wins_without_deduplicating_introspection(self) -> None:
        """Repeated isolation keys stay visible even though lookup selects the nearest one."""
        first, second = RealmId.new(), RealmId.new()
        outer = Scope.child(Scope.root(), IsolateScope(Key("db"), first))
        inner = Scope.child(outer, IsolateScope(Key("db"), second))
        self.assertEqual(inner.own_realm("db"), second)
        self.assertEqual(inner.isolated_keys(), ["db", "db"])
