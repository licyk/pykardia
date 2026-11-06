"""Run the same real-runtime lifecycle scenario as the pinned Rust host fixture."""

import asyncio
import json

from pykardia import Context, Disposer, JsonValue, PluginDef


async def run() -> list[JsonValue]:
    """Observe provider/consumer reload, replacement, cleanup and final resource state."""
    root = Context()
    trace: list[JsonValue] = []

    async def provider(context: Context, config: JsonValue) -> None:
        """Provide one version and record the end of its resource lifetime."""
        context.provide_as("foo", config)
        trace.append(["provider apply", config])
        context.on_dispose(lambda: trace.append(["provider cleanup", config]))

    async def consumer(context: Context, config: JsonValue) -> None:
        """Read actual committed values during both apply and asynchronous cleanup."""
        trace.append(["consumer apply", context.get_as("foo", int).arc(), config])

        async def cleanup() -> None:
            """Yield before observing the old binding to exercise its retention guard."""
            await asyncio.sleep(0)
            trace.append(["consumer cleanup", context.get_as("foo", int).arc()])

        context.defer("read", Disposer.future(cleanup))

    definition = PluginDef.new(consumer).inject(["foo"]).name("consumer")
    dependent = root.plugin_def(definition, "old")
    trace.append(["initial", dependent.state().name])
    supplying = root.plugin_def(PluginDef.new(provider).name("provider"), 1)
    await supplying.settled()
    await dependent.settled()
    await asyncio.gather(supplying.update(2), dependent.update("new"))
    await dependent.settled()

    async def replacement(context: Context, config: JsonValue) -> None:
        """Record the replacement's source without keeping the old dependency."""
        trace.append(["replacement apply", config])

    root.registry().replace(definition, PluginDef.new(replacement))
    await dependent.settled()
    trace.append(["identity", dependent.id().get(), supplying.id().get(), dependent.config()])
    await supplying.restart()
    await root.fiber().dispose()
    trace.append(
        [
            "final",
            root.registry().size(),
            root.fiber().effect_count(),
            len(root.bindings("foo")),
            root.fiber().state().name,
        ]
    )
    return trace


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run()), ensure_ascii=False))
