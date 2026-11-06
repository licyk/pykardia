"""Typed events, listener records and configuration interception hooks."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Generic, TypeVar, cast

from pykardia.core.error import AggregateError
from pykardia.core.key import Key, RealmId
from pykardia.core.status import FiberState
from pykardia.core.value import JsonValue

if TYPE_CHECKING:
    from pykardia.core.context import Context
    from pykardia.core.fiber import Fiber

Output = TypeVar("Output", covariant=True)


class Event(Generic[Output]):
    """An event dispatched by concrete type, with overridable filtering and bail behavior."""

    def filter(self, listener: Context) -> bool:
        """Deliver to every listener unless the event specializes its scope."""
        return True

    def is_bailed(self, output: object) -> bool:
        """Handle unit/bool/value outputs; optional false can override this per event."""
        return output is not None and output is not False


@dataclass
class PluginInserted(Event[None]):
    """A child fiber has entered the registry."""

    fiber: Fiber


@dataclass
class PluginRemoved(Event[None]):
    """A fiber's instantiation has been retired."""

    fiber: Fiber


@dataclass
class StatusChanged(Event[None]):
    """A fiber changed its observable lifecycle state."""

    fiber: Fiber
    old: FiberState
    new: FiberState


@dataclass
class ServiceChanged(Event[None]):
    """A live binding appeared, disappeared or changed provider availability."""

    key: Key
    realm: RealmId

    def filter(self, listener: Context) -> bool:
        """Deliver only to contexts currently resolving this key to the changed realm."""
        return listener.realm(self.key) == self.realm


@dataclass
class ConfigCommitted(Event[None]):
    """An update is about to replace a fiber's source configuration."""

    fiber: Fiber
    config: JsonValue


@dataclass
class Replaced(Event[None]):
    """A fiber was moved to a replacement plugin definition."""

    fiber: Fiber


@dataclass(eq=False)
class Listener:
    """A registration snapshot whose removed flag remains visible during dispatch."""

    context: Context
    callback: Callable[[Event[object]], object]
    asynchronous: bool = False
    removed: bool = False


@dataclass
class UpdateHook:
    """A removable hook offering decoded configurations to a typed callback."""

    accept: Callable[[object], Awaitable[None] | None]
    removed: bool = False

    def offer(self, config: object) -> Awaitable[None] | None:
        """Skip hooks removed after an update took its listener snapshot."""
        return None if self.removed else self.accept(config)


ConfigHook = Callable[["Fiber", JsonValue, Callable[[JsonValue], object]], object]


def listeners_for(context: Context, event: Event[object]) -> list[Listener]:
    """Snapshot registrations and run user filters after leaving the state lock."""
    entries = context._root.read(lambda state: list(state.listeners.get(type(event), [])))
    return [entry for entry in entries if event.filter(entry.context)]


async def finish_listener(future: Awaitable[object]) -> None:
    """Drive a fire-and-forget listener while discarding its return value."""
    await future


async def dispatch_parallel(context: Context, event: Event[object]) -> None:
    """Start synchronous callbacks in order and collect all asynchronous outcomes."""
    tasks: list[Awaitable[object]] = []
    for entry in listeners_for(context, event):
        if entry.removed:
            continue
        try:
            value = entry.callback(event)
        except Exception as error:
            tasks.append(_ready(error))
        else:
            if entry.asynchronous:
                tasks.append(cast(Awaitable[object], value))
            else:
                tasks.append(_ready(value))
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)
    errors = [value for value in outcomes if isinstance(value, BaseException)]
    if errors:
        raise AggregateError(
            f"one or more listeners of event {type(event).__name__} failed", errors
        )


async def _ready(value: object) -> object:
    """Represent an already evaluated synchronous output in a parallel join."""
    return value
