"""Insertion-ordered registrations addressed by monotonically increasing IDs."""

from collections.abc import Iterator
from typing import Generic, TypeVar

T = TypeVar("T")


class IdTable(Generic[T]):
    """Keep independent registrations whose inverses remove only their own entry."""

    def __init__(self) -> None:
        """Start an empty registry; the first inserted entry receives ID one."""
        self._next: int = 0
        self._entries: dict[int, T] = {}

    def insert(self, value: T) -> int:
        """Register a value and return an ID never reused by this table."""
        self._next += 1
        self._entries[self._next] = value
        return self._next

    def remove(self, identity: int) -> T | None:
        """Remove a registration if it still exists."""
        return self._entries.pop(identity, None)

    def get(self, identity: int) -> T | None:
        """Read a registration without changing its order."""
        return self._entries.get(identity)

    def __len__(self) -> int:
        """Return the number of live registrations."""
        return len(self._entries)

    def len(self) -> int:
        """Return the number of live registrations."""
        return len(self)

    def is_empty(self) -> bool:
        """Return whether no registrations remain."""
        return not self._entries

    def drain_rev(self) -> list[T]:
        """Remove all registrations and return their values newest first."""
        values = list(reversed(self._entries.values()))
        self._entries.clear()
        return values

    def iter(self) -> Iterator[tuple[int, T]]:
        """Iterate registrations oldest first."""
        return iter(self._entries.items())

    def values(self) -> Iterator[T]:
        """Iterate registered values oldest first."""
        return iter(self._entries.values())
