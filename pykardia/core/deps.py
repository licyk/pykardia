"""Explicit dependency tuples and declaration-restricted typed contexts."""

from __future__ import annotations

from typing import TYPE_CHECKING, Generic, TypeVar

from pykardia.core.error import Undeclared
from pykardia.core.service import Service, Svc

if TYPE_CHECKING:
    from pykardia.core.context import Context

D = TypeVar("D")
S = TypeVar("S", bound=Service)


class Deps:
    """An ordered tuple of service types declared and resolved together."""

    def __init__(self, *services: type[Service]) -> None:
        """Retain the exact dependency type order, including repeated declarations."""
        self.services = services

    def keys(self) -> list[str]:
        """Return service names in tuple order."""
        return [service.NAME for service in self.services]

    def get(self, context: Context) -> tuple[Svc[Service], ...]:
        """Resolve each member through the context's actual committed view."""
        return tuple(context.get(service) for service in self.services)


class Injected(Generic[D]):
    """A context restricting typed get to an explicitly declared service tuple."""

    def __init__(self, context: Context, dependencies: Deps) -> None:
        """Bind a context to declarations extracted at a plugin's annotation boundary."""
        self._context = context
        self._dependencies = dependencies

    def get(self, service: type[S]) -> Svc[S]:
        """Reject undeclared types before checking the underlying runtime dependency view."""
        if service not in self._dependencies.services:
            raise Undeclared(service.NAME)
        return self._context.get(service)

    def deps(self) -> tuple[Svc[Service], ...]:
        """Resolve all explicitly declared services in order."""
        return self._dependencies.get(self._context)

    def context(self) -> Context:
        """Return the underlying context for effects and other runtime operations."""
        return self._context
