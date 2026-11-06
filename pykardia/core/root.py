"""Shared root state, logging and owned asynchronous transitions."""

from __future__ import annotations

import asyncio
import weakref
from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING, TypeVar

from pykardia.core.effect import EffectCell
from pykardia.core.key import FiberId
from pykardia.core.plugin import PluginDef
from pykardia.core.scope import Scope
from pykardia.core.value import JsonValue

if TYPE_CHECKING:
    from pykardia.core.context import Context
    from pykardia.core.fiber import Fiber
from pykardia.core.logger import Logger, LoggerState
from pykardia.core.runtime import Action, Runtime
from pykardia.core.state import State, Transition

T = TypeVar("T")


class Root:
    """Own one state lock and event loop while keeping user callbacks outside the lock."""

    def __init__(self) -> None:
        """Create a root without requiring a currently running asyncio loop."""
        self.driver: Runtime = Runtime()
        self.state: State = State()
        self.logger: LoggerState = LoggerState()

    def read(self, operation: Callable[[State], T]) -> T:
        """Read state atomically without notifying transition observers."""
        return self.driver.read(lambda: operation(self.state))

    def run(self, operation: Callable[[State], T]) -> T:
        """Commit one state segment, wake observers and execute its queued actions."""

        def segment() -> tuple[T, list[Action]]:
            """Separate state mutation from the transition tasks it schedules."""
            result = operation(self.state)
            transitions, self.state.actions = self.state.actions, []
            return result, [partial(self._execute, action) for action in transitions]

        return self.driver.run(segment)

    def attach(self, identity: FiberId, cell: EffectCell) -> None:
        """Register an effect on its fiber and give it a non-owning removal callback."""
        self.run(lambda state: self.attach_in_state(state, identity, cell))

    def attach_in_state(self, state: State, identity: FiberId, cell: EffectCell) -> None:
        """Register an effect while already inside a mutation segment."""
        reference = weakref.ref(self)
        fiber = state.fibers.get(identity)
        if fiber is None:
            return
        entry = fiber.disposables.insert(cell)

        def detach() -> None:
            """Release the direct registration outside the root's lock."""
            root = reference()
            if root is not None:
                removed = root.run(lambda current: current.detach_effect(identity, entry))
                del removed

        cell.set_detach(detach)

    def context(self) -> Context:
        """Construct the permanent root context without allocating another runtime."""
        from pykardia.core.context import Context

        scope = self.read(lambda state: state.fibers[FiberId.ROOT].scope)
        return Context._from_parts(self, FiberId.ROOT, scope)

    def fiber_at(self, identity: FiberId, scope: Scope) -> Fiber:
        """Construct a handle from a captured scope without acquiring the state lock."""
        from pykardia.core.context import Context
        from pykardia.core.fiber import Fiber

        return Fiber(Context._from_parts(self, identity, scope))

    def resolve_config(self, fiber: Fiber, definition: PluginDef, config: JsonValue) -> object:
        """Run root config hooks outermost first, ending at the definition's decoder."""
        hooks = self.read(lambda state: list(state.config_hooks.values()))

        def resolve(index: int, value: JsonValue) -> object:
            """Pass each hook the remainder of the actual decoding chain."""
            if index == len(hooks):
                return definition.decode(value)
            return hooks[index](fiber, value, lambda next_value: resolve(index + 1, next_value))

        return resolve(0, config)

    async def wait_until(self, predicate: Callable[[State], bool]) -> None:
        """Wait without losing a state change between checking and registering a waiter."""
        await self.driver.wait_until(lambda: predicate(self.state))

    def _execute(self, action: Transition | Callable[[Root], None]) -> None:
        """Start a transition after its synchronous state prefix has committed."""
        if callable(action):
            action(self)
        elif action.kind == "reload":
            self.driver.spawn(self._reload(action.fiber))
        else:
            self.driver.spawn(self._unload(action.fiber))

    async def _reload(self, identity: FiberId) -> None:
        """Complete a root episode after allowing same-turn revisions to coalesce."""
        await asyncio.sleep(0)
        from pykardia.core.fiber import Fiber

        def prepare(state: State) -> tuple[PluginDef | None, JsonValue] | None:
            """Skip an apply superseded during the initial coalescing yield."""
            from copy import deepcopy

            fiber = state.fibers[identity]
            if fiber.episode != fiber.target:
                return None
            return state.def_of(identity), deepcopy(fiber.config)

        job = self.read(prepare)
        error: BaseException | None = None
        if job is not None and job[0] is not None:
            definition, config = job
            assert definition is not None
            fiber = Fiber.of(self, identity)
            try:
                decoded = self.resolve_config(fiber, definition, config)
                await definition.apply(fiber.context(), decoded)
            except (Exception, asyncio.CancelledError) as failure:
                error = failure
        self.run(lambda state: state.finish_reload(identity, error))

    async def _unload(self, identity: FiberId) -> None:
        """Start fiber inverses in reverse order and await all of them concurrently."""
        await self.wait_until(lambda state: state.can_drain(identity))
        cells = self.run(lambda state: state.start_drain(identity))
        disposals = [cell.dispose() for cell in cells]
        results = await asyncio.gather(*(item.wait() for item in disposals), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                Logger.internal(
                    self, self.read(lambda state: state.fiber_name(identity)), identity
                ).log_error(result)
        self.run(lambda state: state.finish_unload(identity))
