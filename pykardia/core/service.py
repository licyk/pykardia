"""Typed service handles and inverse operations for withdrawing provisions."""

from __future__ import annotations

import weakref
from collections.abc import Awaitable, Iterable
from typing import TYPE_CHECKING, Generic, TypeVar

from pykardia.core.cleanup import Disposer
from pykardia.core.key import FiberId, ImplId, Key
from pykardia.core.value import JsonValue

if TYPE_CHECKING:
    from pykardia.core.context import Context
    from pykardia.core.fiber import Fiber
    from pykardia.core.logger import Logger
    from pykardia.core.root import Root

T = TypeVar("T")


class Service:
    """A concrete service class carrying its declared coeffect name."""

    NAME: str

    @staticmethod
    def merge_intercept(values: Iterable[JsonValue]) -> JsonValue:
        """Merge object metadata right-biased, ignoring nonobject entries."""
        from pykardia.core.context import merge_shallow

        return merge_shallow(values)


class Svc(Generic[T]):
    """A value with explicit caller/provider attribution; access the value through arc()."""

    def __init__(
        self, value: T, caller: Context, key: Key, identity: ImplId, provider: FiberId
    ) -> None:
        """Retain the resolved value and the exact caller scope of this access."""
        self._value = value
        self._caller = caller
        self._key = key
        self._identity = identity
        self._provider = provider

    def arc(self) -> T:
        """Return the shared service object without copying or rebinding its methods."""
        return self._value

    def caller(self) -> Context:
        """Return the use-site context that owns work performed on the caller's behalf."""
        return self._caller

    def key(self) -> Key:
        """Return the service key used for this access."""
        return self._key

    def impl_id(self) -> ImplId:
        """Return the committed binding's identity."""
        return self._identity

    def provider(self) -> Fiber:
        """Return the fiber that originally provided this binding."""
        from pykardia.core.fiber import Fiber

        return Fiber.of(self._caller._root, self._provider)

    def logger(self) -> Logger:
        """Use caller logger metadata with the providing plugin as fallback name."""
        from pykardia.core.logger import Logger

        name = self._caller._root.read(lambda state: state.fiber_name(self._provider))
        return Logger.for_caller(self._caller, name)

    def intercepts(self) -> list[JsonValue]:
        """Read admitting inject metadata first, then caller overlays outermost first."""
        context = self._caller

        def declared() -> tuple[bool, JsonValue]:
            """Snapshot metadata from the declaration admitting this access."""
            state = context._root.state
            located = state.locate(context, self._key)
            if located is not None and located.declared is not None:
                inject = state.fibers[located.declared].inject
                return inject.has_meta(self._key), inject.meta(self._key)
            return False, None

        present, metadata = context._root.driver.read(declared)
        return ([metadata] if present else []) + context.intercepts(self._key)

    def meta(self) -> JsonValue:
        """Merge interception metadata through the service type's declared strategy."""
        if isinstance(self._value, Service):
            return self._value.merge_intercept(self.intercepts())
        return Service.merge_intercept(self.intercepts())

    def __repr__(self) -> str:
        """Display the underlying value, matching the reference handle's debug behavior."""
        return repr(self._value)


def withdraw(root: Root, identity: ImplId) -> Disposer:
    """Build an inverse that frees the slot now and retires it after consumers drain."""
    reference = weakref.ref(root)

    async def finish(current: Root | None) -> None:
        """Keep old values accessible until all committed consumers finish their inverses."""
        if current is not None:
            await current.wait_until(
                lambda state: identity not in state.impls or not state.impls[identity].dependents
            )
            current.run(lambda state: state.retire_provision(identity))

    def inverse() -> Awaitable[None]:
        """Withdraw synchronously before returning the asynchronous binding guard."""
        current = reference()
        if current is not None:
            current.run(lambda state: state.unbind(identity))
        return finish(current)

    return Disposer.future(inverse)
