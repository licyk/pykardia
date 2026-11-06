"""Exercise real HMR unit abort, independent commit, recovery and host exclusion."""

from dataclasses import dataclass
from pathlib import Path

from pykardia import Context, Event, JsonValue, PluginDef
from pykardia.hmr import Exports, Hmr, HmrExt, Imports, ModuleCode, ModuleGraph, ModuleSource
from pykardia.loader import Loader
from tests.hmr.common import APP


class Files(ModuleSource):
    """Keep the actual mutable module source used by the transaction probe."""

    def __init__(self) -> None:
        """Start an empty source table."""
        self.codes: dict[str, ModuleCode] = {}

    def load(self, id: str) -> ModuleCode:
        """Return current code or the same source error as the Rust fixture."""
        try:
            return self.codes[id]
        except KeyError as error:
            raise ValueError("missing module") from error


@dataclass
class Query(Event[JsonValue]):
    """Ask one running plugin for its current imported version."""

    name: str


def code(name: str, dep: str | None, version: str, fail: bool = False) -> ModuleCode:
    """Build current code exporting a real event-answering plugin or an evaluation failure."""

    def evaluate(imports: Imports) -> Exports:
        """Read actual module imports before constructing a fresh plugin definition."""
        if fail:
            raise ValueError("broken evaluation")
        value = imports.value(dep) if dep is not None else version

        async def apply(ctx: Context, config: JsonValue) -> None:
            """Register a real listener owned by this plugin episode."""
            ctx.on(Query, lambda query: value if query.name == name else None)

        return Exports.plugin(PluginDef.new(apply))

    return ModuleCode.new([dep] if dep is not None else [], evaluate)


def data(version: str) -> ModuleCode:
    """Export a dependency value through the actual graph evaluator."""
    return ModuleCode.new([], lambda _: Exports.value(version))


async def run() -> list[JsonValue]:
    """Run the same public operations and observations as the pinned Rust probe."""
    root = Context()
    files = Files()
    graph = ModuleGraph(files)
    trace: list[JsonValue] = []
    files.codes[f"{APP}/dep"] = data("v1")
    for name in ["left", "right"]:
        files.codes[f"{APP}/{name}"] = code(name, f"{APP}/dep", "")
    files.codes[f"{APP}/other"] = code("other", None, "v1")
    await root.plugin_def(Loader.plugin(graph)).settled()
    await root.plugin_def(Hmr.plugin(graph)).settled()
    loader = root.get(Loader).arc()
    hmr = HmrExt(root.get(Hmr))
    try:
        for name in ["left", "right", "other"]:
            await loader.create({"id": name, "name": f"{APP}/{name}", "config": {"marker": name}})
        await loader.settled()
        original = [loader.resolve(id).inner_fiber() for id in ["left", "right", "other"]]
        trace.append(
            [
                "initial",
                list(graph.loaded()),
                root.bail(Query("left")),
                root.bail(Query("right")),
                root.bail(Query("other")),
            ]
        )
        for stage in ["abort shared", "recover", "skip host"]:
            if stage == "abort shared":
                files.codes[f"{APP}/dep"] = data("v2")
                files.codes[f"{APP}/left"] = code("left", f"{APP}/dep", "", True)
                files.codes[f"{APP}/other"] = code("other", None, "v2")
                paths = [f"{APP}/dep", f"{APP}/left", f"{APP}/other"]
            elif stage == "recover":
                files.codes[f"{APP}/left"] = code("left", f"{APP}/dep", "")
                paths = [f"{APP}/dep"]
            else:
                graph.mark_host([f"{APP}/other"])
                files.codes[f"{APP}/other"] = code("other", None, "v3")
                paths = [f"{APP}/other"]

            async def roots(paths: list[str] = paths) -> list[Path]:
                """Supply this stage's roots from inside the transaction callback."""
                return [Path(path) for path in paths]

            result = await hmr.reconcile(roots)
            settled: list[JsonValue] = [fiber in result.settled_fibers for fiber in original]
            failures: list[JsonValue] = [
                [list(failure.modules), str(failure.error)] for failure in result.import_failures
            ]
            identity: list[JsonValue] = [
                loader.resolve(id).inner_fiber() == fiber
                for id, fiber in zip(["left", "right", "other"], original, strict=True)
            ]
            trace.append(
                [
                    stage,
                    settled,
                    failures,
                    list(result.skipped_host_modules),
                    identity,
                    root.bail(Query("left")),
                    root.bail(Query("right")),
                    root.bail(Query("other")),
                    graph.import_(f"{APP}/dep").json_value,
                    loader.root().data(),
                ]
            )
    finally:
        await root.fiber().dispose()
    trace.append(
        ["shutdown", root.bail(Query("left")), root.bail(Query("right")), root.bail(Query("other"))]
    )
    return trace
