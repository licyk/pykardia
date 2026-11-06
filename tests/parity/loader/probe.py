"""Drive the Python loader through the same public operations as host.rs."""

from pykardia import Context, JsonValue, PluginDef
from pykardia.loader import Loader, builtins


def plugin(trace: list[JsonValue], label: str) -> PluginDef:
    """Create a plugin that records actual application and disposal."""

    async def apply(ctx: Context, config: JsonValue) -> None:
        """Record this episode and retain its cleanup inverse."""
        trace.append([label, "apply", config])
        ctx.on_dispose(lambda: trace.append([label, "dispose"]))

    return PluginDef.new(apply)


async def run() -> list[JsonValue]:
    """Observe source preservation, moves, rename, group disable and plugin writeback."""
    root = Context()
    trace: list[JsonValue] = []
    catalog = builtins()
    catalog.register_def("foo", plugin(trace, "foo"))
    catalog.register_def("bar", plugin(trace, "bar"))
    await root.plugin_def(Loader.plugin(catalog)).settled()
    loader = root.get(Loader).arc()
    await loader.create({"id": "g", "name": "group", "group": True, "config": []})
    await loader.create({"id": "x", "name": "foo", "config": {"v": 1}, "custom": "keep"})
    await loader.settled()
    entry = loader.resolve("x")
    fiber = entry.inner_fiber()
    assert fiber is not None
    trace.append(["source", loader.root().data()])
    await loader.update("x", {}, parent="g")
    await loader.settled()
    trace.append(["move identity", entry == loader.resolve("x"), fiber == entry.inner_fiber()])
    await loader.update("x", {"config": {"v": 2}})
    await loader.settled()
    await loader.update("x", {"name": "bar"})
    await loader.settled()
    trace.append(["rename identity", fiber == entry.inner_fiber()])
    await loader.update("g", {"disabled": True})
    await loader.settled()
    trace.append(["disabled", entry.is_disabled(), loader.root().data()])
    await loader.update("g", {"disabled": None})
    await loader.settled()
    current = entry.inner_fiber()
    assert current is not None
    await current.update({"v": 3})
    trace.append(["self update", entry.options()])
    await current.dispose()
    await loader.settled()
    trace.append(["self dispose", entry.options()])
    loader.remove("g")
    await loader.settled()
    trace.append(["empty", loader.root().data(), len(loader.entries())])
    await root.fiber().dispose()
    return trace
