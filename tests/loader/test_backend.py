"""Tree backend routing and source-location diagnostics."""

from pathlib import Path

from pykardia import Context, Disposer, Issue, JsonValue, PluginDef, ValidationError
from pykardia.core.config import PathSegment
from pykardia.loader import (
    MISSING,
    Catalog,
    Location,
    ModuleNotFound,
    TreeBackend,
    TreeHandle,
    value_at,
)
from pykardia.loader.backend import Missing
from pykardia.loader.expr import ExprError
from tests.loader.common import LoaderCase


class LocatedBackend(TreeBackend):
    """Provide deterministic external positions without replacing loader behavior."""

    def __init__(self, catalog: Catalog) -> None:
        """Retain the actual resolver and record location boundary arguments."""
        self.catalog = catalog
        self.calls: list[tuple[str, list[PathSegment], JsonValue | Missing]] = []

    def resolver(self) -> Catalog:
        """Resolve from the tree's catalog."""
        return self.catalog

    def base_dir(self) -> Path:
        """Return the lexical base supplied by this backend."""
        return Path("/configs/../configs")

    def handle(self) -> object:
        """Expose the actual backend owner."""
        return self

    def locate(
        self, id: str, path: list[PathSegment], value: JsonValue | Missing = MISSING
    ) -> Location:
        """Record value presence and return a stable source position."""
        self.calls.append((id, path, value))
        return Location(Path("source.yml"), 4 if path else 2, 7 if path else 3)

    def locate_expr(self, id: str, path: list[PathSegment], expr: str, offset: int) -> Location:
        """Expose the delegated expression offset as a character column."""
        return Location(Path("source.yml"), 5, 8 + offset)


class BackendTests(LoaderCase):
    """Exercise public backend hooks through real owned trees."""

    async def asyncSetUp(self) -> None:
        """Create a tree with an independent resolver and inherited nested base."""
        await super().asyncSetUp()
        self.local = Catalog()
        self.located = LocatedBackend(self.local)
        self.trees: list[TreeHandle] = []

        async def apply(ctx: Context, config: JsonValue) -> None:
            """Create an owned tree and retain its stop inverse."""
            tree = self.loader.create_tree(ctx, self.located)
            self.trees.append(tree)
            ctx.defer("tree", Disposer.new(tree.root().stop))

        self.catalog.register_def("tree", PluginDef.new(apply).inject(["loader"]))
        await self.loader.create({"id": "tree", "name": "tree"})
        await self.loader.settled()
        self.tree = self.trees[0]

    async def test_resolver_base_handle_and_location_forwarding(self) -> None:
        """Backend resolution and metadata remain local to their owned tree."""
        counter = self.register("local")
        self.local.register_def("only-here", counter.definition())
        await self.tree.create({"id": "x", "name": "only-here"})
        await self.loader.settled()
        self.assertEqual(counter.applied, 1)
        self.assertIs(self.tree.handle(), self.located)
        self.assertEqual(self.tree.base_dir(), Path("/configs/../configs"))
        entry = self.tree.resolve("x")
        fiber = entry.inner_fiber()
        assert fiber is not None
        nested = self.loader.create_tree(fiber.context(), TreeBackend())
        self.assertEqual(nested.base_dir(), self.tree.base_dir())
        self.assertEqual(
            self.tree.locate("x", ["config"], None), Location(Path("source.yml"), 4, 7)
        )
        self.tree.locate("x", ["missing"])
        self.assertEqual(
            self.located.calls[-2:], [("x", ["config"], None), ("x", ["missing"], MISSING)]
        )
        self.assertEqual(str(self.tree.locate_expr("x", ["disabled"], "foo", 2)), "source.yml:5:10")
        self.assertIs(value_at({"x": [None]}, ["x", 0]), None)
        paths: list[list[PathSegment]] = [["x", -1], ["x", 1], ["absent"], ["x", True]]
        for path in paths:
            self.assertIs(value_at({"x": [None]}, path), MISSING)

    async def test_diagnostics_preserve_value_presence_and_ancestor_frames(self) -> None:
        """Errors gain field and ancestor positions only when the backend supplies them."""
        options: JsonValue = {"id": "x", "name": "missing", "config": {"a": None}}
        group = self.tree.root()._id
        missing = self.loader.locate_error(group, options, ModuleNotFound("missing"))
        self.assertEqual(
            str(missing),
            "Cannot find plugin 'missing' (at name, source.yml:4:7)\n    at x (source.yml:2:3)",
        )
        invalid = ValidationError([Issue("bad null", ["a"]), Issue("missing", ["b"])])
        located = self.loader.locate_error(group, options, invalid)
        self.assertEqual(
            str(located),
            "invalid config:\n  - bad null (at a, source.yml:4:7)"
            "\n  - missing (at b, source.yml:4:7)\n    at x (source.yml:2:3)",
        )
        self.assertIn(("x", ["config", "a"], None), self.located.calls)
        self.assertIn(("x", ["config", "b"], MISSING), self.located.calls)
        failed = ExprError(["config", "a"], "foo", 1, "unavailable")
        self.assertEqual(
            str(self.loader.locate_error(group, options, failed)),
            f"{failed}\n    at config.a (source.yml:5:9)\n    at x (source.yml:2:3)",
        )
        self.assertIs(self.loader.locate_error(self.loader.root()._id, options, invalid), invalid)
        plain = ValueError("runtime")
        self.assertIs(self.loader.locate_error(group, options, plain), plain)
