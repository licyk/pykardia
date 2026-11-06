"""Observable graph caching, dependency boundaries and synchronous resolver behavior."""

import unittest

from pykardia import Context, JsonValue, PluginDef
from pykardia.hmr import ExportKind, Exports, Imports, ModuleCode, ModuleGraph, ModuleSource


class MemorySource(ModuleSource):
    """Store current code and observe real graph load requests."""

    def __init__(self) -> None:
        """Start with an empty source and no load requests."""
        self.codes: dict[str, ModuleCode] = {}
        self.loads: list[str] = []

    def load(self, id: str) -> ModuleCode:
        """Read the current source snapshot, recording the graph's requested order."""
        self.loads.append(id)
        return self.codes[id]


async def noop(ctx: Context, config: JsonValue) -> None:
    """Provide an actual no-op plugin definition for export identity checks."""


class GraphTests(unittest.IsolatedAsyncioTestCase):
    """Exercise the production graph without duplicating its traversal algorithm."""

    def setUp(self) -> None:
        """Create a fresh source and graph for every cache scenario."""
        self.source = MemorySource()
        self.graph = ModuleGraph(self.source)

    async def test_dependency_order_cache_and_json_snapshots(self) -> None:
        """Dependencies load once in order and exported JSON snapshots cannot mutate the cache."""
        self.source.codes["dep"] = ModuleCode.new([], lambda _: Exports.value({"x": [1]}))
        self.source.codes["main"] = ModuleCode.new(
            ["dep", "dep"], lambda imports: Exports.value(imports.value("dep"))
        )
        result = self.graph.import_("main")
        self.assertEqual(self.source.loads, ["main", "dep"])
        self.assertEqual(self.graph.loaded(), ["dep", "main"])
        self.assertEqual(self.graph.edges("main"), ["dep", "dep"])
        assert isinstance(result.json_value, dict)
        result.json_value["x"] = 2
        self.assertEqual(self.graph.import_("main").json_value, {"x": [1]})
        self.assertEqual(self.source.loads, ["main", "dep"])

    async def test_cycles_see_unevaluated_modules_and_imports_are_declared(self) -> None:
        """A static cycle exposes no partial record and undeclared cached modules stay hidden."""
        observed: list[object] = []
        self.source.codes["hidden"] = ModuleCode.new([], lambda _: Exports.value(9))
        self.graph.import_("hidden")

        def evaluate(imports: Imports) -> Exports:
            """Record actual dependency views while the other cycle participant is loading."""
            observed.extend([imports.get("a"), imports.get("hidden")])
            return Exports.value(2)

        self.source.codes["a"] = ModuleCode.new(
            ["b"], lambda imports: Exports.value(imports.value("b"))
        )
        self.source.codes["b"] = ModuleCode.new(["a"], evaluate)
        self.assertEqual(self.graph.import_("a").json_value, 2)
        self.assertEqual(observed, [None, None])
        self.assertEqual(self.graph.loaded(), ["hidden", "b", "a"])

    async def test_failure_retries_but_keeps_successful_dependencies(self) -> None:
        """Failed evaluation is never cached, while its successfully loaded dependency remains."""
        self.source.codes["dep"] = ModuleCode.new([], lambda _: Exports.none())

        def fail(imports: Imports) -> Exports:
            """Raise an evaluation failure at the real external module boundary."""
            raise ValueError("evaluation failed")

        self.source.codes["main"] = ModuleCode.new(["dep"], fail)
        with self.assertRaisesRegex(ValueError, "evaluation failed"):
            self.graph.import_("main")
        self.assertEqual(self.graph.loaded(), ["dep"])
        self.source.codes["main"] = ModuleCode.new(["dep"], lambda _: Exports.value(None))
        self.assertEqual(self.graph.import_("main").kind, ExportKind.VALUE)
        self.assertEqual(self.source.loads, ["main", "dep", "main"])

    async def test_resolve_captures_synchronously_and_rejects_nonplugin_exports(self) -> None:
        """Resolve evaluates before returning its awaitable and reports captured failures later."""
        definition = PluginDef.new(noop)
        self.source.codes["plugin"] = ModuleCode.new([], lambda _: Exports.plugin(definition))
        future = self.graph.resolve("plugin")
        self.assertTrue(self.graph.is_loaded("plugin"))
        self.source.codes.clear()
        self.assertIs(await future, definition)
        self.source.codes["data"] = ModuleCode.new([], lambda _: Exports.value(None))
        with self.assertRaisesRegex(ValueError, "module data does not export a plugin"):
            await self.graph.resolve("data")
        with self.assertRaises(KeyError):
            await self.graph.resolve("missing")

    async def test_backup_restoration_consumes_record_and_preserves_host_marks(self) -> None:
        """Abort restoration reinstates old exports, appends their slot and consumes the backup."""
        for id in ["a", "b"]:
            self.source.codes[id] = ModuleCode.new([], lambda _: Exports.value(1))
            self.graph.import_(id)
        self.graph.mark_host(["a", "unloaded"])
        backup = self.graph._evict("a")
        self.source.codes["a"] = ModuleCode.new([], lambda _: Exports.value(2))
        self.graph.import_("a")
        self.graph._restore("a", backup)
        self.assertEqual(self.graph.loaded(), ["b", "a"])
        self.assertEqual(self.graph.import_("a").json_value, 1)
        self.assertTrue(self.graph.is_host("a"))
        self.assertTrue(self.graph.is_host("unloaded"))
        self.graph._restore("a", backup)
        self.assertFalse(self.graph.is_loaded("a"))
        self.assertEqual(self.graph.edges("a"), [])
