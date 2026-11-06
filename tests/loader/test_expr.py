"""Expression integration with real checked service access and source paths."""

import unittest

from pykardia import Context, Inactive, JsonValue, Undeclared
from pykardia.loader.expr import ExprError, evaluate, evaluate_at, has_expr, interpolate, truthy


class ExprTests(unittest.IsolatedAsyncioTestCase):
    """Exercise source expression cases through actual service provision and dependencies."""

    async def test_evaluates_javascript_like_expressions(self) -> None:
        """Evaluate the source's operator examples against a real service JSON view."""
        root = Context()
        root.provide_as(
            "settings", {"enabled": True, "port": 8080, "tags": ["a", "b"], "name": "app"}
        )
        cases: list[tuple[str, JsonValue]] = [
            ("1 + 2 * 3", 7),
            ("'#' + 1", "#1"),
            ("!settings.enabled", False),
            ("settings.port + 1", 8081),
            ("settings.tags[1] + settings.tags.length", "b2"),
            ("settings.missing ?? 'fallback'", "fallback"),
            ("settings.missing?.deep", None),
            ("settings.port > 1000 && settings.name === 'app' ? 'big' : 'small'", "big"),
            ("[1, 'x', { a: settings.port }]", [1, "x", {"a": 8080}]),
            ("1 == '1' && 1 !== '1' && null == undefined", True),
            ("7 % 4 - -1 / 2", 3.5),
        ]
        for source, expected in cases:
            self.assertEqual(evaluate(root, source), expected)
        await root.fiber().dispose()

    async def test_reports_syntax_errors_and_access_errors(self) -> None:
        """Keep raw access errors for evaluate and add field locations for interpolation."""
        root = Context()
        root.provide_as("settings", {"port": 8080})
        for source in ("1 +", "'open"):
            with self.assertRaises(ValueError):
                evaluate(root, source)
        with self.assertRaises(Inactive):
            evaluate(root, "unknown.value")
        self.assertEqual(
            interpolate(root, {"a": [{"__jsExpr": "settings.port"}], "b": 1}), {"a": [8080], "b": 1}
        )
        with self.assertRaises(ExprError) as caught:
            evaluate_at(root, "'🙂' + missing", ["config", 2])
        self.assertEqual((caught.exception.path, caught.exception.offset), (["config", 2], 6))
        self.assertTrue(has_expr({"a": [{"__jsExpr": "settings.port"}]}))
        self.assertFalse(has_expr({"__jsExpr": 1}))
        await root.fiber().dispose()

    async def test_short_circuit_and_checked_injection(self) -> None:
        """Unused branches avoid reads while evaluated identifiers require declarations."""
        root = Context()

        async def provide(ctx: Context) -> None:
            """Make the service available from a sibling rather than an owning ancestor."""
            ctx.provide_as("settings", {"port": 8080})

        await root.inject([], provide).settled()

        async def consume(ctx: Context) -> None:
            """Observe access failures from inside an actual undeclared plugin fiber."""
            self.assertEqual(evaluate(ctx, "true || settings.port"), True)
            self.assertEqual(evaluate(ctx, "null?.[settings.port]"), None)
            with self.assertRaises(Undeclared):
                evaluate(ctx, "settings.port")

        await root.inject([], consume).settled()
        await root.fiber().dispose()

    def test_truthiness_and_snapshot(self) -> None:
        """Empty collections stay truthy and interpolation does not mutate source containers."""
        self.assertTrue(truthy([]))
        self.assertTrue(truthy({}))
        self.assertFalse(truthy(""))
        source: JsonValue = {"a": [{"__jsExpr": "1 + 2"}], "b": {"__jsExpr": 42}}
        self.assertEqual(interpolate(Context(), source), {"a": [3], "b": {"__jsExpr": 42}})
        self.assertEqual(source, {"a": [{"__jsExpr": "1 + 2"}], "b": {"__jsExpr": 42}})
