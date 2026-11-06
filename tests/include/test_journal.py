"""Journal conflict resolution and actual nested entry mutation."""

import unittest

from pykardia import JsonValue
from pykardia.include.journal import (
    Changes,
    Conflict,
    Journal,
    Remove,
    Upsert,
    apply_journal,
    diff_options,
    merge,
    merge_records,
    reconcile,
    record,
)
from pykardia.include.tree import detach, entry_mut, find, flatten, place
from pykardia.loader import MISSING


class JournalTests(unittest.TestCase):
    """Port the source journal cases and test presence/order through real tree helpers."""

    def test_merges_records(self) -> None:
        """Merge edits and moves, preserving original creation and cancelling unwritten entries."""
        self.assertIsNone(merge_records(Upsert(True, None, 0, {}), Remove()))
        self.assertEqual(merge_records(Upsert(False, MISSING, 0, {}), Remove()), Remove())
        self.assertEqual(
            merge_records(Remove(), Upsert(True, MISSING, 0, {"a": 1})),
            Upsert(False, MISSING, 0, {"a": 1}),
        )
        self.assertEqual(
            merge_records(
                Upsert(True, "g", 2, {"a": 1, "b": 1}), Upsert(False, MISSING, 0, {"b": 2})
            ),
            Upsert(True, "g", 2, {"a": 1, "b": 2}),
        )

    def test_applies_a_journal(self) -> None:
        """Moves and insertions modify actual group members at their recorded positions."""
        data: list[JsonValue] = [
            {"id": "a", "name": "foo"},
            {"id": "g", "name": "group", "group": True, "config": []},
        ]
        journal: Journal = {
            "a": Upsert(False, "g", 0, {"config": {"x": 1}}),
            "b": Upsert(True, None, 0, {"name": "bar"}),
        }
        warnings: list[str] = []
        result = apply_journal(data, journal, lambda id, key: True, warnings.append)
        self.assertEqual(
            result,
            [
                {"id": "b", "name": "bar"},
                {
                    "id": "g",
                    "name": "group",
                    "group": True,
                    "config": [{"id": "a", "name": "foo", "config": {"x": 1}}],
                },
            ],
        )
        self.assertEqual(warnings, [])
        self.assertEqual(data[0], {"id": "a", "name": "foo"})

    def test_the_file_wins_conflicts(self) -> None:
        """The file wins overlapping keys and removals while independent changes remain."""
        base = flatten([{"id": "a", "name": "foo", "config": 1}, {"id": "b", "name": "foo"}])
        theirs = flatten([{"id": "a", "name": "foo", "config": 2}])
        journal: Journal = {
            "a": Upsert(False, MISSING, 0, {"config": 3, "disabled": True}),
            "b": Upsert(False, MISSING, 0, {"config": 3}),
            "c": Upsert(True, None, 0, {"name": "new"}),
        }
        conflicts = reconcile(journal, base, theirs, lambda id, key: True)
        self.assertEqual(
            conflicts,
            [
                Conflict("a", 'key "config" modified both in file and at runtime'),
                Conflict("b", "removed in file, modified at runtime"),
            ],
        )
        self.assertEqual(list(journal), ["a", "c"])
        self.assertEqual(journal["a"], Upsert(False, MISSING, 0, {"disabled": True}))

    def test_record_order_deletion_and_null_presence(self) -> None:
        """Missing values delete while explicit null remains a written field."""
        changes = diff_options({"id": "a", "a": 1, "b": None}, {"id": "a", "a": None, "c": 2})
        self.assertEqual(changes, {"a": None, "b": MISSING, "c": 2})
        journal: Journal = {}
        record(journal, "a", {"id": "a", "name": "foo"}, None, False, None, 0)
        record(journal, "b", {"id": "b", "name": "bar"}, None, False, None, 0)
        merge(journal, "a", Upsert(False, MISSING, 0, changes))
        self.assertEqual(list(journal), ["b", "a"])
        record(journal, "b", None, {"id": "b"}, False, None, 0)
        self.assertNotIn("b", journal)
        result = apply_journal([], journal, lambda id, key: True, lambda text: None)
        self.assertEqual(result, [{"id": "a", "name": "foo", "a": None, "c": 2}])

    def test_missing_move_target_restores_position_and_keeps_fields(self) -> None:
        """A failed move warns and restores source position after applying admitted edits."""
        data: list[JsonValue] = [{"id": "a", "x": 1}, {"id": "b"}]
        warnings: list[str] = []
        journal: Journal = {"a": Upsert(False, "absent", 0, {"x": 2, "y": 3})}
        result = apply_journal(data, journal, lambda id, key: key != "y", warnings.append)
        self.assertEqual(result, [{"id": "a", "x": 2}, {"id": "b"}])
        self.assertEqual(warnings, ["cannot move entry a: group absent not found"])

    def test_reconcile_moves_removals_and_patch_owned_fields(self) -> None:
        """Only file-owned mutations conflict and matching edits do not produce warnings."""
        base = flatten(
            [
                {"id": "a", "x": None},
                {"id": "b"},
                {"id": "g", "group": True, "config": [{"id": "c"}]},
            ]
        )
        theirs = flatten([{"id": "a", "x": 1}, {"id": "b", "extra": 1}, {"id": "c"}, {"id": "new"}])
        changes: Changes = {"x": 2, "y": None}
        journal: Journal = {
            "a": Upsert(False, MISSING, 0, changes),
            "b": Remove(),
            "c": Upsert(False, "elsewhere", 0, {}),
            "new": Remove(),
            "gone": Remove(),
        }
        conflicts = reconcile(journal, base, theirs, lambda id, key: not (id == "a" and key == "x"))
        self.assertEqual(
            [c.reason for c in conflicts],
            [
                "modified in file, removed at runtime",
                "moved both in file and at runtime",
                "added in file, removed at runtime",
            ],
        )
        self.assertEqual(list(journal), ["a"])
        self.assertEqual(journal["a"], Upsert(False, MISSING, 0, changes))

    def test_tree_paths_and_duplicate_snapshot_order(self) -> None:
        """Lookup picks the first duplicate while flatten overwrites its snapshot in place."""
        data: list[JsonValue] = [
            {"id": "a", "v": 1},
            {"id": "g", "group": True, "config": [{"id": "a", "v": 2}, {"id": "b"}]},
        ]
        flat = flatten(data)
        self.assertEqual(list(flat), ["a", "g", "b"])
        self.assertEqual(flat["a"].parent, "g")
        self.assertEqual(find(data, "a"), [0])
        self.assertEqual(detach(data, "a"), {"id": "a", "v": 1})
        self.assertEqual(find(data, "a"), [0, 0])
        self.assertTrue(place(data, {"id": "z"}, "g", 99))
        self.assertEqual(find(data, "z"), [0, 2])
        self.assertIsNone(entry_mut(data, "absent"))
        self.assertFalse(place(data, {"id": "q"}, "b", 0))
