"""Port the source entry-option validation, mutation and typed-view cases."""

import unittest

from pykardia import JsonValue
from pykardia.loader import (
    EntryOptions,
    EntryValidationError,
    diff_keys,
    expr_source,
    is_expr,
    merge,
    sort_keys,
    validate_options,
)


class OptionsTests(unittest.TestCase):
    """Keep validation at the boundary and retain unknown fields in source objects."""

    def test_accepts_valid_options(self) -> None:
        """Known optional nulls and unknown fields leave usable entries valid."""
        cases: list[JsonValue] = [
            {"name": "foo"},
            {
                "id": "a",
                "name": "foo",
                "group": False,
                "disabled": {"__jsExpr": "true"},
                "inject": ["bar"],
                "intercept": {"bar": {}},
                "isolate": {"bar": True, "baz": "label"},
                "config": {"__jsExpr": "whatever"},
            },
            {"name": "foo", "group": None, "disabled": None, "inject": None, "future": [1]},
            {"name": "foo", "inject": {"bar": {"required": True}}},
            {"name": "group", "group": True, "config": []},
        ]
        for value in cases:
            self.assertEqual(validate_options(value, False), [], value)

    def test_rejects_missing_or_invalid_fields(self) -> None:
        """Field diagnostics and ordering match the Rust boundary validator."""
        cases: list[tuple[JsonValue, str]] = [
            (None, "entry must be an object"),
            (["foo"], "entry must be an object"),
            ({}, "name is required"),
            ({"id": 1, "name": "foo"}, "id must be a string"),
            ({"id": "a:b", "name": "foo"}, 'id must not contain ":"'),
            ({"name": "g", "group": True}, "config is required"),
            ({"name": "g", "group": True, "config": {}}, "config must be an array"),
            ({"name": "foo", "inject": ["bar", 1]}, "inject.1 must be a string"),
            ({"name": "foo", "inject": "bar"}, "inject must be an array or an object"),
            ({"name": "foo", "intercept": []}, "intercept must be an object"),
            ({"name": "foo", "isolate": {"bar": False}}, "isolate.bar must be true or a string"),
        ]
        for value, expected in cases:
            self.assertEqual(
                [issue.message for issue in validate_options(value, False)], [expected]
            )

    def test_accepts_an_expression_only_for_disabled(self) -> None:
        """Expression markers are not ordinary option objects, even with invalid source types."""
        expression: JsonValue = {"__jsExpr": "true"}
        issues = validate_options(
            {key: expression for key in ("name", "group", "inject", "intercept", "isolate")}, False
        )
        self.assertEqual(
            [issue.format_path() for issue in issues],
            ["name", "group", "inject", "intercept", "isolate"],
        )
        self.assertEqual(len(validate_options({"name": "foo", "disabled": "yes"}, False)), 1)
        self.assertEqual(
            len(validate_options({"name": "foo", "disabled": {"__jsExpr": 1}}, False)), 1
        )
        self.assertTrue(is_expr({"__jsExpr": None}))
        self.assertIsNone(expr_source({"__jsExpr": None}))

    def test_validates_group_children_deeply(self) -> None:
        """Deep runtime validation adds the full config/index path to nested issues."""
        options: JsonValue = {"name": "g", "group": True, "config": [{"name": 1}]}
        self.assertEqual(validate_options(options, False), [])
        issues = validate_options(options, True)
        self.assertEqual(issues[0].format_path(), "config.0.name")
        self.assertEqual(
            str(EntryValidationError(issues)),
            "invalid entry options:\n  - name must be a string (at config.0.name)",
        )

    def test_merges_sorts_and_diffs(self) -> None:
        """Patch deletion, stable output order and typed JSON comparisons follow the source."""
        options: dict[str, JsonValue] = {
            "config": 1,
            "name": "foo",
            "zeta": 1,
            "id": "a",
            "alpha": 2,
        }
        merge(options, {"zeta": None, "beta": 3})
        sort_keys(options)
        self.assertEqual(list(options), ["id", "name", "alpha", "beta", "config"])
        self.assertEqual(
            diff_keys(options, {"id": "a", "name": "foo", "alpha": 2, "config": 2}),
            ["beta", "config"],
        )
        self.assertEqual(
            diff_keys({"a": True, "b": 1, "c": None}, {"a": 1, "b": 1.0, "d": None}),
            ["a", "b", "c", "d"],
        )
        merge(None, {"ignored": True})
        sort_keys(None)

    def test_read_preserves_source_and_declared_metadata(self) -> None:
        """Typed option snapshots do not remove unknown fields or alias mutable source values."""
        source: dict[str, JsonValue] = {
            "id": "entry",
            "name": "plugin",
            "config": {"value": 1},
            "future": [1, 2],
            "disabled": {"__jsExpr": "settings.off"},
            "inject": {"a": None, "b": {}},
            "isolate": {"a": True, "b": "shared"},
            "intercept": {"a": {"key": 1}},
        }
        self.assertEqual(validate_options(source, True), [])
        options = EntryOptions.read(source)
        self.assertEqual(
            (options.id, options.name, options.disabled), ("entry", "plugin", "settings.off")
        )
        self.assertEqual(list(options.inject.keys()), ["a", "b"])
        self.assertFalse(options.inject.has_meta("a"))
        self.assertTrue(options.inject.has_meta("b"))
        self.assertEqual(options.isolate, [("a", None), ("b", "shared")])
        merge(source, {"config": 2})
        self.assertEqual(options.config, {"value": 1})
        self.assertEqual(source["future"], [1, 2])
