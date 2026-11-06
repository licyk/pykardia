"""Run dependency waiting, reactive updates, implementation replacement and cleanup."""

import asyncio
from dataclasses import dataclass

from pykardia import Context, Injected, Service, plugin, service


@service(name="greeting")
@dataclass
class Greeting(Service):
    """A service whose identity changes when its provider reloads."""

    text: str


@dataclass
class Config:
    """Typed provider configuration; unknown JSON fields remain tolerated."""

    text: str = "hello"


@plugin(provide=["greeting"])
async def provider(ctx: Context, config: Config) -> None:
    """Provide the configured greeting and collect its cleanup."""
    ctx.provide(Greeting(config.text))
    ctx.on_dispose(lambda: print("provider disposed"))


@plugin(provide=["greeting"])
async def replacement(ctx: Context, config: Config) -> None:
    """Provide a different implementation while retaining the existing fiber's configuration."""
    ctx.provide(Greeting(config.text.upper()))
    ctx.on_dispose(lambda: print("replacement disposed"))


@plugin
async def consumer(ctx: Injected[tuple[Greeting]]) -> None:
    """Read the committed dependency and release before its provider is withdrawn."""
    value = ctx.get(Greeting).arc().text
    print(f"consumer sees {value}")
    ctx.context().on_dispose(lambda: print(f"consumer released {value}"))


async def main() -> None:
    """Exercise the production runtime and always await deterministic resource cleanup."""
    root = Context()
    try:
        dependent = root.plugin_def(consumer)
        await dependent.settled()
        print(f"before provider: {dependent.state().value}")
        source = root.plugin_def(provider, {"text": "hello"})
        await source.settled()
        await dependent.settled()
        await source.update({"text": "updated"})
        await dependent.settled()
        root.registry().replace(provider, replacement)
        await source.settled()
        await dependent.settled()
    finally:
        await root.fiber().dispose()


if __name__ == "__main__":
    asyncio.run(main())
