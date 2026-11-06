"""Core schema, original source positions and deterministic YAML formatting."""

import unittest

from pykardia import JsonValue
from pykardia.include.yaml import ParseError, Position, dump, parse


class YamlTests(unittest.TestCase):
    """Port the source YAML unit cases without duplicating parser logic."""

    def test_parses_the_core_schema_with_expressions_and_aliases(self) -> None:
        """Aliases retain anchor locations and tagged scalars point at the actual value."""
        source = (
            "- id: a\n  name: foo\n  disabled: !!js 'x > 1'\n"
            "  config: &c { a: 1, b: [1, 2.5, ~, true, 'q', 0x1f] }\n- *c\n- !!str 12\n"
        )
        value, syntax = parse(source)
        self.assertEqual(
            value,
            [
                {
                    "id": "a",
                    "name": "foo",
                    "disabled": {"__jsExpr": "x > 1"},
                    "config": {"a": 1, "b": [1, 2.5, None, True, "q", 31]},
                },
                {"a": 1, "b": [1, 2.5, None, True, "q", 31]},
                "12",
            ],
        )
        self.assertEqual(syntax.locate([0, "name"]), Position(2, 9))
        self.assertEqual(syntax.locate([0, "config"]), Position(4, 3))
        self.assertEqual(syntax.locate([0, "missing"]), Position(1, 3))
        self.assertEqual(syntax.locate([1, "a"]), Position(4, 19))
        self.assertEqual(syntax.locate([0, "disabled"]), Position(3, 18))
        self.assertEqual(syntax.locate_expr([0, "disabled"], 4), Position(3, 23))

    def test_reports_errors_and_unknown_tags(self) -> None:
        """Malformed source has character positions while unknown tags fail explicitly."""
        with self.assertRaises(ParseError) as failed:
            parse("a: [1, 2\nb: c")
        self.assertEqual(failed.exception.position.line, 2)
        with self.assertRaisesRegex(ParseError, "unresolved tag"):
            parse("- !custom x")

    def test_dumps_values_that_read_back_the_same(self) -> None:
        """The source block indentation and expression quoting round-trip real JSON values."""
        value: JsonValue = [
            {
                "id": "a",
                "name": "foo",
                "config": {
                    "text": "it's: here",
                    "n": "12",
                    "empty": "",
                    "list": [1, {"x": None}],
                    "map": {},
                },
            },
            {
                "id": "b",
                "name": "group",
                "group": True,
                "config": [{"id": "c", "name": "bar", "disabled": {"__jsExpr": "a && b"}}],
            },
            {"id": "d", "name": "multi", "config": "line\nbreak"},
        ]
        text = dump(value)
        self.assertEqual(parse(text)[0], value)
        self.assertTrue(
            text.startswith(
                "- id: a\n  name: foo\n  config:\n    text: 'it''s: here'\n    n: '12'\n"
            )
        )
        self.assertIn("disabled: !!js 'a && b'", text)

    def test_schema_ignores_version_and_reads_only_first_document(self) -> None:
        """Version declarations never enable YAML 1.1 coercions or merge-key behavior."""
        value, _ = parse(
            "%YAML 1.1\n---\nvalue: [yes, 0777, 0b101]\n<<: {merged: true}\n---\ninvalid: ["
        )
        self.assertEqual(value, {"value": ["yes", 777, "0b101"], "<<": {"merged": True}})
