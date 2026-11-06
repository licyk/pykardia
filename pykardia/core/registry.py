"""Plugin registry handles and parent-owned instantiation effects."""

from __future__ import annotations

import weakref
from collections.abc import Awaitable
from typing import TYPE_CHECKING

from pykardia.core.cleanup import Disposer
from pykardia.core.effect import EffectCell
from pykardia.core.fiber import Fiber
from pykardia.core.key import FiberId
from pykardia.core.plugin import PluginDef, RuntimeKey
from pykardia.core.state import State
from pykardia.core.value import JsonValue

if TYPE_CHECKING:
    from pykardia.core.context import Context
    from pykardia.core.root import Root


class Registry:
    """Queries and replacement operations on one root's ordered plugin registry."""

    def __init__(self, context: Context) -> None:
        """Bind operations to the context's root."""
        self._context = context

    def size(self) -> int:
        """Count definitions with at least one registered fiber."""
        return self._context._root.read(lambda state: len(state.runtimes))

    def keys(self) -> list[RuntimeKey]:
        """Snapshot keys in registration order."""
        return self._context._root.read(lambda state: list(state.runtimes))

    def get(self, definition: PluginDef) -> RuntimeKey | None:
        """Resolve aliases and return the key only when it is registered."""

        def lookup(state: State) -> RuntimeKey | None:
            """Resolve one definition under the root lock."""
            key, _ = state.forward(definition)
            return key if key in state.runtimes else None

        return self._context._root.read(lookup)

    def has(self, definition: PluginDef) -> bool:
        """Return whether a definition or its replacement has live instances."""
        return self.get(definition) is not None

    def fibers(self, definition: PluginDef) -> list[Fiber]:
        """Snapshot instances in registration order after following replacements."""

        def lookup(state: State) -> list[FiberId]:
            """Copy IDs without constructing handles while holding the lock."""
            key, _ = state.forward(definition)
            runtime = state.runtimes.get(key)
            return [] if runtime is None else list(runtime.fibers)

        root = self._context._root
        return [Fiber.of(root, identity) for identity in root.read(lookup)]

    def replace(self, old: PluginDef, new: PluginDef) -> list[Fiber]:
        """Replace current instances in place and forward future uses of the old definition."""
        root = self._context._root
        identities = root.run(lambda state: state.replace(old, new))
        return [Fiber.of(root, identity) for identity in identities]


def instantiate(parent: Context, definition: PluginDef, config: JsonValue) -> Fiber:
    """Insert a child and register its retirement inverse atomically with its parent."""
    root = parent._root

    def insert(state: State) -> Fiber:
        """Complete registration before any insertion event can reenter the runtime."""
        identity = state.insert_fiber(parent, definition, config)
        cell = EffectCell(root.driver, "ctx.plugin()")
        cell.push_disposer(retire(root, identity))
        root.attach_in_state(state, parent._fiber, cell)
        fiber = state.fibers[identity]
        fiber.retire = cell
        state.warn_cycle(identity)
        return root.fiber_at(identity, fiber.scope)

    return root.run(insert)


def retire(root: Root, identity: FiberId) -> Disposer:
    """Build the parent effect's synchronous retirement and owned transition observer."""
    reference = weakref.ref(root)

    async def finish(current: Root | None) -> None:
        """Collect a retired fiber only after its current transition finishes."""
        if current is not None:
            await current.wait_until(
                lambda state: identity not in state.fibers or not state.fibers[identity].inertia
            )
            current.run(lambda state: state.collect_removed(identity))

    def inverse() -> Awaitable[None]:
        """Retire immediately even when callers drop the returned disposal."""
        current = reference()
        if current is not None:
            current.run(lambda state: state.retire_fiber(identity))
        return finish(current)

    return Disposer.future(inverse)
