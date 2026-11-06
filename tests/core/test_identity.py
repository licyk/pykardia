"""Identity and registry tests traced to core/src/key.rs and id_table.rs."""

import concurrent.futures
import unittest

from pykardia.core.id_table import IdTable
from pykardia.core.key import FiberId, ImplId, Key, RealmId


class IdentityTests(unittest.TestCase):
    """Check generative realms, distinct kinds and never-reused registration IDs."""

    def test_ids_are_never_reused_and_drain_is_lifo(self) -> None:
        """Port id_table::ids_are_never_reused_and_drain_is_lifo."""
        table: IdTable[str] = IdTable()
        first = table.insert("a")
        second = table.insert("b")
        self.assertEqual(table.remove(first), "a")
        third = table.insert("c")
        self.assertGreater(third, second)
        self.assertIsNone(table.remove(first))
        self.assertEqual(list(table.iter()), [(second, "b"), (third, "c")])
        self.assertEqual(list(table.values()), ["b", "c"])
        self.assertEqual(table.drain_rev(), ["c", "b"])
        self.assertTrue(table.is_empty())
        self.assertGreater(table.insert("d"), third)

    def test_identity_kinds_and_string_keys(self) -> None:
        """Keep identity kinds distinct while allowing service-key string lookup."""
        self.assertEqual(FiberId.ROOT.get(), 0)
        self.assertNotEqual(FiberId(1), ImplId(1))
        self.assertEqual(repr(FiberId(1)), "fiber#1")
        self.assertEqual(repr(ImplId(1)), "impl#1")
        self.assertEqual(str(ImplId(1)), "1")
        table: dict[str, int] = {Key.new("a.b"): 42}
        self.assertEqual(table["a.b"], 42)
        self.assertEqual(Key(" 服务 ").as_str(), " 服务 ")

    def test_realms_are_globally_fresh_across_threads(self) -> None:
        """Allocate realms concurrently without reusing the global isolation symbol."""
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(RealmId.new) for _ in range(100)]
            realms = [future.result() for future in futures]
        self.assertEqual(len(set(realms)), 100)
        self.assertEqual(realms[0], realms[0])
        self.assertTrue(repr(realms[0]).startswith("realm#"))
