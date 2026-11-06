"""Fiber handles and root-episode resource lifecycle operations."""

from __future__ import annotations

from collections.abc import Awaitable, Generator
from copy import deepcopy
from typing import TYPE_CHECKING

from pykardia.core.cleanup import Disposal
from pykardia.core.effect import EffectHandle, EffectMeta
from pykardia.core.events import ConfigCommitted, UpdateHook
from pykardia.core.key import FiberId
from pykardia.core.plugin import Inject, PluginDef, RuntimeKey
from pykardia.core.runtime import Completion
from pykardia.core.scope import Scope
from pykardia.core.state import State
from pykardia.core.status import FiberState
from pykardia.core.value import JsonValue

if TYPE_CHECKING:
    from pykardia.core.context import Context
    from pykardia.core.root import Root


class Fiber:
    """A root-owned fiber handle whose identity survives lifecycle transitions."""

    def __init__(self, context: Context) -> None:
        """Retain the fiber's own context, independent of a derived caller scope."""
        self._context: Context = context

    @classmethod
    def of(cls, root: Root, identity: FiberId) -> Fiber:
        """Create a handle using the existing fiber scope or a detached fallback scope."""
        from pykardia.core.context import Context

        scope = root.read(
            lambda state: state.fibers[identity].scope if identity in state.fibers else None
        )
        return cls(
            Context._from_parts(root, identity, scope if scope is not None else Scope.root())
        )

    def id(self) -> FiberId:
        """Return the fiber's root-local identity."""
        return self._context.fiber_id()

    def context(self) -> Context:
        """Return the fiber's own context."""
        return self._context

    def state(self) -> FiberState:
        """Return Disposed after the root has collected the fiber record."""
        return self._context._root.read(
            lambda state: (
                state.fibers[self.id()].state if self.id() in state.fibers else FiberState.Disposed
            )
        )

    def is_removed(self) -> bool:
        """Return whether the instantiation has been retired."""
        return self._context._root.read(
            lambda state: state.fibers[self.id()].removed if self.id() in state.fibers else True
        )

    def is_root(self) -> bool:
        """Return whether this is the permanent root fiber."""
        return self.id() == FiberId.ROOT

    def config(self) -> JsonValue:
        """Return an independent source-configuration snapshot."""
        return self._context._root.read(
            lambda state: (
                deepcopy(state.fibers[self.id()].config) if self.id() in state.fibers else None
            )
        )

    def inject(self) -> Inject:
        """Snapshot the current definition's dependency declaration."""
        return self._context._root.read(
            lambda state: (
                state.fibers[self.id()].inject.copy() if self.id() in state.fibers else Inject()
            )
        )

    def runtime(self) -> RuntimeKey | None:
        """Return this instance's current plugin identity."""
        return self._context._root.read(
            lambda state: state.fibers[self.id()].runtime if self.id() in state.fibers else None
        )

    def def_(self) -> PluginDef | None:
        """Return the current definition; the underscore escapes the Python keyword."""
        return self._context._root.read(lambda state: state.def_of(self.id()))

    def name(self) -> str:
        """Return the plugin name or nearest named instantiating ancestor."""
        return self._context._root.read(lambda state: state.fiber_name(self.id()))

    def update(self, config: JsonValue) -> Awaitable[None]:
        """Validate loaded configuration before committing it and offering update hooks."""
        return self.update_with(config, False)

    def update_with(self, config: JsonValue, no_save: bool) -> Awaitable[None]:
        """Replace the source configuration; no_save suppresses its write-back event."""
        root, identity = self._context._root, self.id()
        config = deepcopy(config)

        def prepare(state: State) -> tuple[bool, PluginDef | None, list[UpdateHook]]:
            """Capture hooks while checking whether prevalidation applies."""
            state.assert_active(identity)
            fiber = state.fibers[identity]
            return (
                fiber.target is not None,
                state.def_of(identity),
                list(fiber.update_hooks.values()),
            )

        loaded, definition, hooks = root.read(prepare)
        decoded = (
            root.resolve_config(self, definition, deepcopy(config))
            if loaded and definition is not None
            else None
        )
        if not no_save:
            root.context().emit(ConfigCommitted(self, deepcopy(config)))

        def commit(state: State) -> None:
            """Publish configuration after listeners observed the previous source value."""
            fiber = state.fibers.get(identity)
            if fiber is not None:
                fiber.config = config
                fiber.error = None

        root.run(commit)
        if loaded and definition is not None:
            for hook in hooks:
                handled = hook.offer(decoded)
                if handled is not None:
                    return handled
        return self.restart()

    def error(self) -> BaseException | None:
        """Return the current episode's error without changing its identity."""
        return self._context._root.read(
            lambda state: state.fibers[self.id()].error if self.id() in state.fibers else None
        )

    def parent(self) -> Fiber | None:
        """Return the instantiating fiber; the root is its own parent."""
        root = self._context._root
        parent = root.read(
            lambda state: state.fibers[self.id()].parent if self.id() in state.fibers else None
        )
        return None if parent is None else Fiber.of(root, parent)

    def effect_handle(self) -> EffectHandle | None:
        """Return the instantiation effect when this fiber has a parent-owned registration."""
        cell = self._context._root.read(
            lambda state: state.fibers[self.id()].retire if self.id() in state.fibers else None
        )
        return None if cell is None else EffectHandle(cell)

    def effects(self) -> list[EffectMeta]:
        """Return only directly owned effect trees, in registration order."""
        cells = self._context._root.read(
            lambda state: (
                list(state.fibers[self.id()].disposables.values())
                if self.id() in state.fibers
                else []
            )
        )
        return [cell.meta() for cell in cells]

    def effect_count(self) -> int:
        """Count the fiber accumulator without counting adopted descendants twice."""
        return self._context._root.read(
            lambda state: (
                len(state.fibers[self.id()].disposables) if self.id() in state.fibers else 0
            )
        )

    def settled(self) -> Settle:
        """Create a lazy observer that can be dropped without cancelling transitions."""
        return Settle(self)

    async def _settle(self) -> None:
        """Wait until no transition remains, then report the current stored failure."""
        identity, root = self.id(), self._context._root
        await root.wait_until(
            lambda state: identity not in state.fibers or not state.fibers[identity].inertia
        )
        error = self.error()
        if error is not None:
            raise error

    def restart(self) -> Awaitable[None]:
        """Advance the revision now, returning an observer for the transition."""
        try:
            self._context._root.run(lambda state: state.restart(self.id()))
        except Exception as error:
            return Completion.failed(error)
        return self.settled()

    def dispose(self) -> Disposal:
        """Retire a child; for the root, start a restart and retain its permanent identity."""
        if self.is_root():
            restart = self.restart()
            return Disposal(self._context._root.driver.spawn(_finish_root_disposal(restart)))
        handle = self.effect_handle()
        return Disposal(Completion.ready(None)) if handle is None else handle.dispose()

    def __eq__(self, other: object) -> bool:
        """Compare fibers by root identity and fiber ID, not current state."""
        return (
            isinstance(other, Fiber)
            and self._context.same_root(other._context)
            and self.id() == other.id()
        )

    def __repr__(self) -> str:
        """Describe the identity and current lifecycle state."""
        return f"Fiber({self.id()!r}, {self.state().name})"


async def _finish_root_disposal(restart: Awaitable[None]) -> None:
    """Observe root restart without exposing a stored episode error as a disposal error."""
    try:
        await restart
    except Exception:
        pass


class Settle:
    """A lazy observer; constructing or dropping it does not create a coroutine."""

    def __init__(self, fiber: Fiber) -> None:
        """Keep the observed handle alive without acquiring transition ownership."""
        self._fiber = fiber

    def __await__(self) -> Generator[object, None, None]:
        """Wait for the fiber's latest settled state when the observer is actually polled."""
        return self._fiber._settle().__await__()
