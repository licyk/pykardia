"""Service keys and the distinct identities used by fibers, bindings and realms."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import ClassVar

from typing_extensions import Self


class Key(str):
    """A service name with ordinary string equality and hashing."""

    @classmethod
    def new(cls, name: str) -> Key:
        """Construct a key without normalizing or restricting its spelling."""
        return cls(name)

    def as_str(self) -> str:
        """Return this key as a string."""
        return str(self)


@dataclass(frozen=True, repr=False)
class _Identity:
    """An immutable identity whose equality remains specific to its concrete type."""

    _value: int
    _prefix: ClassVar[str]

    def get(self) -> int:
        """Return the numeric identity allocated by its root."""
        return self._value

    def __str__(self) -> str:
        """Display the numeric identity."""
        return str(self._value)

    def __repr__(self) -> str:
        """Display the identity's kind as well as its numeric value."""
        return f"{self._prefix}#{self._value}"


@dataclass(frozen=True, order=True, repr=False)
class FiberId(_Identity):
    """A never-reused fiber identity within a root; zero denotes the root fiber."""

    _prefix = "fiber"
    ROOT: ClassVar[FiberId]


FiberId.ROOT = FiberId(0)


@dataclass(frozen=True, order=True, repr=False)
class ImplId(_Identity):
    """A provider binding identity, distinct even when successive service values compare equal."""

    _prefix = "impl"


_realm_lock = threading.Lock()
_realm_counter: int = 0


def _next_realm() -> int:
    """Allocate a process-wide fresh realm without relying on interpreter atomicity."""
    global _realm_counter
    with _realm_lock:
        _realm_counter += 1
        return _realm_counter


@dataclass(frozen=True, order=True, repr=False)
class RealmId:
    """A generative realm shared only through explicit reuse of its identity."""

    _value: int = field(default_factory=_next_realm)

    @classmethod
    def new(cls) -> Self:
        """Allocate a fresh isolation symbol."""
        return cls()

    def __repr__(self) -> str:
        """Display the reference runtime's realm debug representation."""
        return f"realm#{self._value}"
