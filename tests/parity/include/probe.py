"""Drive real nested includes through the operations recorded by the Rust probe."""

import tempfile
from pathlib import Path

from pykardia import Context, JsonValue, PluginDef
from pykardia.include import Include, IncludePlugin
from pykardia.loader import Loader, builtins


def files(trace: list[JsonValue], base: Path, stage: str) -> None:
    """Record physical file bytes as text without normalizing their formatting."""
    trace.append([stage, (base / "outer.yml").read_text(), (base / "inner.yml").read_text()])


async def settle(loader: Loader, outer: Include, inner: Include) -> None:
    """Flush actual child and parent ownership writes before checking active fibers."""
    await inner.flush()
    await outer.flush()
    await loader.settled()


async def run() -> list[JsonValue]:
    """Observe parent patch persistence, self updates, patch removal and file writes."""
    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary)
        (base / "inner.yml").write_text("# original\n- id: a\n  name: worker\n  config: 1\n")
        (base / "outer.yml").write_text(
            "- id: child\n  name: include\n  config:\n    path: inner.yml\n"
            "    patches:\n      - id: a\n        config: 2\n"
        )
        root = Context()
        trace: list[JsonValue] = []

        async def apply(ctx: Context, config: JsonValue) -> None:
            """Record real lifecycle events from one running worker."""
            trace.append(["apply", config])
            ctx.on_dispose(lambda: trace.append(["dispose"]))

        catalog = builtins()
        catalog.register(IncludePlugin)
        catalog.register_def("worker", PluginDef.new(apply))
        await root.plugin_def(Loader.plugin(catalog)).settled()
        loader = root.get(Loader).arc()
        try:
            await loader.create(
                {
                    "id": "inc",
                    "name": "include",
                    "config": {"path": str(base / "outer.yml")},
                }
            )
            await loader.settled()
            outer_tree = loader.resolve("inc").subtree()
            inner_tree = loader.resolve("inc:child").subtree()
            assert outer_tree is not None and inner_tree is not None
            outer, inner = Include.of(outer_tree), Include.of(inner_tree)
            assert outer is not None and inner is not None
            files(trace, base, "initial")
            original = loader.resolve("inc:child").inner_fiber()
            await loader.update("inc:child:a", {"config": 3})
            await settle(loader, outer, inner)
            files(trace, base, "patch write")
            trace.append(
                ["include identity", original == loader.resolve("inc:child").inner_fiber()]
            )
            worker = loader.resolve("inc:child:a").inner_fiber()
            assert worker is not None
            await worker.update(4)
            await settle(loader, outer, inner)
            files(trace, base, "self update")
            await loader.update("inc:child", {"config": {"path": "inner.yml"}})
            await settle(loader, outer, inner)
            files(trace, base, "remove patch")
            await loader.update("inc:child:a", {"config": 5})
            await settle(loader, outer, inner)
            files(trace, base, "file write")
        finally:
            await root.fiber().dispose()
        files(trace, base, "shutdown")
        return trace
