"""Real loader fixtures and lifecycle observation helpers."""

import asyncio
import unittest
from collections.abc import Awaitable
from dataclasses import dataclass, field

import async_timeout

from pykardia import Context, Fiber, JsonValue, PluginDef
from pykardia.loader import Catalog, EntryChange, Loader, Resolver, TreeBackend, builtins


@dataclass
class Counter:
    """Record actual plugin applications, disposals and decoded configs."""

    applied: int = 0
    disposed: int = 0
    configs: list[JsonValue] = field(default_factory=list)

    def definition(self) -> PluginDef:
        """Build a real plugin retaining this counter as its observation sink."""

        async def apply(ctx: Context, config: JsonValue) -> None:
            """Record application and register an actual inverse for disposal."""
            self.applied += 1
            self.configs.append(config)

            def dispose() -> None:
                """Count this episode's real cleanup."""
                self.disposed += 1

            ctx.on_dispose(dispose)

        return PluginDef.new(apply)


class Recorded(TreeBackend):
    """Capture real backend notifications after tree mutations."""

    def __init__(self) -> None:
        """Start with no observed source mutations."""
        self.changes: list[EntryChange] = []

    def commit(self, change: EntryChange) -> None:
        """Retain the actual mutation record without changing loader behavior."""
        self.changes.append(change)


@dataclass
class Hold:
    """Control one resolver name at the actual asynchronous import boundary."""

    name: str
    skip: int = 0
    fail: bool = False
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)


class HeldResolver(Resolver):
    """A source-style resolver that can pause before returning its catalog snapshot."""

    def __init__(self, catalog: Catalog) -> None:
        """Resolve from the actual mutable catalog with no initial gate."""
        self.catalog = catalog
        self.hold: Hold | None = None

    def resolve(self, name: str) -> Awaitable[PluginDef]:
        """Capture the definition now and apply a matching gate before observing it."""
        hold = self.hold if self.hold is not None and self.hold.name == name else None
        if hold is not None and hold.skip:
            hold.skip -= 1
            hold = None
        result = self.catalog.resolve(name)

        async def wait() -> PluginDef:
            """Wait at the external resolver boundary without replacing loader behavior."""
            if hold is not None:
                hold.entered.set()
                await hold.release.wait()
                if hold.fail:
                    raise ValueError(f"cannot find plugin {name}")
            return await result

        return wait()


class LoaderCase(unittest.IsolatedAsyncioTestCase):
    """Install a real loader and clean all runtime resources after each scenario."""

    root: Context
    catalog: Catalog
    loader: Loader

    async def asyncSetUp(self) -> None:
        """Create the root, built-ins and a recording persistence backend."""
        self.deadline = async_timeout.timeout(8)
        await self.deadline.__aenter__()
        self.root = Context()
        self.catalog = builtins()
        self.resolver = HeldResolver(self.catalog)
        self.backend = Recorded()
        await self.root.plugin_def(Loader.plugin_with(self.resolver, self.backend)).settled()
        self.loader = self.root.get(Loader).arc()

    async def asyncTearDown(self) -> None:
        """Retire the root and await all effect inverses before closing the loop."""
        if not self.deadline.expired:
            await self.root.fiber().dispose()
        await self.deadline.__aexit__(None, None, None)

    async def read(self, values: list[JsonValue]) -> None:
        """Reconcile actual source data and wait for its fibers to settle."""
        await self.loader.root().update(values)
        await self.loader.settled()

    def fiber(self, id: str) -> Fiber:
        """Require the real inner fiber of a successfully created entry."""
        fiber = self.loader.resolve(id).inner_fiber()
        assert fiber is not None
        return fiber

    def register(self, name: str) -> Counter:
        """Register a counting plugin and return its observation sink."""
        counter = Counter()
        self.catalog.register_def(name, counter.definition())
        return counter
