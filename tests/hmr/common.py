"""Actual loader, HMR, graph and event-based plugins for transaction acceptance."""

import unittest
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import async_timeout

from pykardia import Context, Event, JsonValue, PluginDef
from pykardia.hmr import Exports, Hmr, HmrExt, Imports, ModuleCode, ModuleGraph, ReconcileResult
from pykardia.loader import Loader
from tests.hmr.test_graph import MemorySource

APP = Path("/app").absolute().as_posix()


@dataclass
class Query(Event[JsonValue]):
    """Ask an actual running plugin to return its version-dependent value."""

    name: str


def answering(
    name: str, value: JsonValue, body: Callable[[Context], Awaitable[None]] | None = None
) -> PluginDef:
    """Build a plugin whose real listener survives only for its current episode."""

    async def apply(ctx: Context, config: JsonValue) -> None:
        """Register the real query listener before the optional application behavior."""
        ctx.on(Query, lambda query: value if query.name == name else None)
        if body is not None:
            await body(ctx)

    return PluginDef.new(apply).name(name)


def plugin(name: str, value: JsonValue, deps: list[str] | None = None) -> ModuleCode:
    """Evaluate a fresh plugin definition with the declared absolute dependencies."""
    return ModuleCode.new(deps or [], lambda _: Exports.plugin(answering(name, value)))


def dependent(name: str, dep: str) -> ModuleCode:
    """Read a dependency's real exported value while evaluating a fresh plugin."""
    return ModuleCode.new(
        [dep], lambda imports: Exports.plugin(answering(name, imports.value(dep)))
    )


def value(value: JsonValue) -> ModuleCode:
    """Return a module exporting a fresh JSON snapshot."""
    return ModuleCode.new([], lambda _: Exports.value(value))


def throws(message: str, deps: list[str] | None = None) -> ModuleCode:
    """Build source code that fails at its actual evaluation boundary."""

    def evaluate(imports: Imports) -> Exports:
        """Raise the requested module error after its dependencies have loaded."""
        raise ValueError(message)

    return ModuleCode.new(deps or [], evaluate)


class HmrCase(unittest.IsolatedAsyncioTestCase):
    """Install the production services and expose source mutation conveniences."""

    async def asyncSetUp(self) -> None:
        """Create an isolated root with an actual module resolver and HMR provider."""
        self.root = Context()
        self.source = MemorySource()
        self.graph = ModuleGraph(self.source)
        await self.root.plugin_def(Loader.plugin(self.graph)).settled()
        await self.root.plugin_def(Hmr.plugin(self.graph)).settled()
        self.loader = self.root.get(Loader).arc()
        self.hmr = HmrExt(self.root.get(Hmr))

    async def asyncTearDown(self) -> None:
        """Dispose every real plugin and listener before closing the event loop."""
        async with async_timeout.timeout(8):
            await self.root.fiber().dispose()

    async def load(self, name: str, code: ModuleCode) -> str:
        """Install and load a real module through a loader entry."""
        id = f"{APP}/{name}"
        self.source.codes[id] = code
        await self.loader.create({"id": name, "name": id})
        await self.loader.settled()
        return id

    def query(self, name: str) -> JsonValue:
        """Observe the current running episode through its real event registration."""
        return self.root.bail(Query(name))

    async def reconcile(self, *paths: str) -> ReconcileResult:
        """Provide roots inside the actual transaction callback with a bounded deadline."""

        async def roots() -> list[Path]:
            """Return requested paths when the transaction acquires its queue slot."""
            return [Path(path) for path in paths]

        async with async_timeout.timeout(5):
            return await self.hmr.reconcile(roots)
