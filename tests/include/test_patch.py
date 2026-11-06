"""Patch overlays, stable source IDs and journal ownership routing."""

import unittest
from copy import deepcopy

from pykardia import JsonValue
from pykardia.include.journal import Journal, Remove, Upsert
from pykardia.include.patch import (
    FILE,
    EmbeddedOwner,
    PatchOwner,
    apply_patches,
    ensure_insert_ids,
    route_journal,
)
from pykardia.include.tree import flatten
from pykardia.loader import MISSING


class PatchTests(unittest.TestCase):
    """Exercise real patch and journal functions against source-owned trees."""

    def test_inserts_and_overrides(self) -> None:
        """Later patches can address inserted entries and name mismatches own nothing."""
        data: list[JsonValue] = [
            {"id": "a", "name": "foo"},
            {"id": "g", "name": "group", "group": True, "config": []},
        ]
        patches: list[JsonValue] = [
            {"insert": [{"id": "x", "name": "bar"}]},
            {"id": "g", "insert": [{"id": "y", "name": "baz"}]},
            {"id": "a", "config": {"k": 1}},
            {"id": "y", "disabled": True},
            {"id": "a", "name": "wrong", "config": 2},
        ]
        warnings: list[str] = []
        result = apply_patches(data, patches, warnings.append)
        self.assertEqual(
            result.data,
            [
                {"id": "a", "name": "foo", "config": {"k": 1}},
                {
                    "id": "g",
                    "name": "group",
                    "group": True,
                    "config": [{"id": "y", "name": "baz", "disabled": True}],
                },
                {"id": "x", "name": "bar"},
            ],
        )
        self.assertEqual(
            warnings, ["patch: name mismatch for a (expected foo, got wrong), skipping"]
        )
        self.assertEqual(result.index.entry("a"), FILE)
        self.assertEqual(result.index.key("a", "config"), PatchOwner(2))
        self.assertEqual(result.index.entry("x"), EmbeddedOwner(0, "insert"))
        self.assertEqual(result.index.key("y", "disabled"), PatchOwner(3))
        self.assertTrue(result.index.file_owned("a", "name"))
        self.assertFalse(result.index.file_owned("x", None))
        self.assertEqual(data[0], {"id": "a", "name": "foo"})

    def test_routes_changes_to_their_owners(self) -> None:
        """A single runtime update may route fields to both the file and a patch."""
        data: list[JsonValue] = [{"id": "a", "name": "foo"}]
        patches: list[JsonValue] = [
            {"insert": [{"id": "x", "name": "bar"}]},
            {"id": "a", "config": 1},
        ]
        journal: Journal = {
            "a": Upsert(False, MISSING, 0, {"config": 2, "disabled": True}),
            "x": Upsert(False, MISSING, 0, {"config": 3}),
        }
        self.assertTrue(route_journal(journal, data, patches, lambda text: None))
        self.assertEqual(data, [{"id": "a", "name": "foo", "disabled": True}])
        self.assertEqual(
            patches,
            [{"insert": [{"id": "x", "name": "bar", "config": 3}]}, {"id": "a", "config": 2}],
        )

    def test_replaced_group_config_ids_and_later_patch(self) -> None:
        """Replacing members erases their old owners and persists anonymous new IDs on source."""
        data: list[JsonValue] = [{"id": "g", "group": True, "config": [{"id": "old"}]}]
        patches: list[JsonValue] = [
            {"id": "old", "disabled": True},
            {
                "id": "g",
                "config": [
                    {"name": "anon"},
                    {"id": "new", "group": True, "config": [{"id": "nested"}]},
                ],
            },
            {"id": "nested", "config": 42},
        ]
        first = apply_patches(data, patches, lambda text: None)
        second = apply_patches(data, patches, lambda text: None)
        self.assertEqual(first.data, second.data)
        self.assertNotIn("old", first.index.owners)
        self.assertNotIn("old", first.index.keys)
        flat = flatten(first.data)
        anonymous = next(id for id in flat if id not in ("g", "new", "nested"))
        self.assertRegex(anonymous, "^[0-9a-f]{8}$")
        self.assertEqual(first.index.entry(anonymous), EmbeddedOwner(1, "config"))
        self.assertEqual(first.index.key("nested", "config"), PatchOwner(2))

    def test_insert_ids_and_invalid_patch_warnings(self) -> None:
        """IDs are added to nested insert source while invalid patches leave the tree unchanged."""
        patches: list[JsonValue] = [
            {"insert": [{"name": "group", "group": True, "config": [{"name": "foo"}]}]}
        ]
        ensure_insert_ids(patches, lambda id: False)
        before = deepcopy(patches)
        ensure_insert_ids(patches, lambda id: False)
        self.assertEqual(patches, before)
        result = apply_patches([], patches, lambda text: None)
        self.assertEqual(len(flatten(result.data)), 2)
        warnings: list[str] = []
        invalid: list[JsonValue] = [
            42,
            {},
            {"id": "missing"},
            {"id": "missing", "insert": []},
            {"id": "a", "insert": []},
        ]
        result = apply_patches([{"id": "a"}], invalid, warnings.append)
        self.assertEqual(result.data, [{"id": "a"}])
        self.assertEqual(
            warnings,
            [
                "patch: id is required for non-insert patches",
                "patch: entry missing not found",
                "patch insert: entry missing not found",
                "patch insert: entry a is not a group",
            ],
        )

    def test_routes_moves_across_file_insert_and_config_owners(self) -> None:
        """Moving across ownership carries complete options and refreshes subsequent routing."""
        data: list[JsonValue] = [
            {"id": "a", "name": "foo"},
            {"id": "g", "group": True, "config": []},
        ]
        patches: list[JsonValue] = [
            {"insert": [{"id": "p", "group": True, "config": []}]},
            {"id": "g", "config": [{"id": "b", "name": "bar"}]},
        ]
        journal: Journal = {
            "a": Upsert(False, "p", 0, {"config": 1}),
            "b": Upsert(False, None, 0, {"config": 2}),
            "new": Upsert(True, "g", 0, {"name": "new"}),
        }
        self.assertTrue(route_journal(journal, data, patches, lambda text: None))
        flat = flatten(apply_patches(data, patches, lambda text: None).data)
        self.assertEqual(flat["a"].parent, "p")
        self.assertIsNone(flat["b"].parent)
        self.assertEqual(flat["new"].parent, "g")
        self.assertEqual(flat["a"].options, {"id": "a", "name": "foo", "config": 1})
        self.assertTrue(
            route_journal({"a": Remove(), "new": Remove()}, data, patches, lambda text: None)
        )
        self.assertNotIn("a", flatten(apply_patches(data, patches, lambda text: None).data))

    def test_null_in_patch_owned_change_removes_override(self) -> None:
        """The reference routes both null and deleted patch-owned fields to removing the key."""
        data: list[JsonValue] = [{"id": "a", "config": 1}]
        patches: list[JsonValue] = [{"id": "a", "config": 2}]
        self.assertTrue(
            route_journal(
                {"a": Upsert(False, MISSING, 0, {"config": None})}, data, patches, lambda text: None
            )
        )
        self.assertEqual(patches, [{"id": "a"}])
        self.assertEqual(apply_patches(data, patches, lambda text: None).data, data)
