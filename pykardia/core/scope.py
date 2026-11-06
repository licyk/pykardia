"""Mutable context derivation chains with isolation and interception overlays."""

from __future__ import annotations

import threading
from collections.abc import Iterator, Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import TypeAlias

from pykardia.core.key import FiberId, Key, RealmId
from pykardia.core.value import JsonValue


class Overlay:
    """Shared mutable context metadata; edits do not automatically refresh consumers."""

    def __init__(self, tag: int | None = None) -> None:
        """Create independent metadata tables and an optional immutable opaque tag."""
        self._tag: int | None = tag
        self._lock = threading.Lock()
        self._isolations: dict[Key, RealmId] = {}
        self._intercepts: dict[Key, JsonValue] = {}

    @property
    def tag(self) -> int | None:
        """Return the immutable tag supplied when this overlay was created."""
        return self._tag

    @classmethod
    def new(cls) -> Overlay:
        """Create an untagged overlay."""
        return cls()

    @classmethod
    def tagged(cls, tag: int) -> Overlay:
        """Create an overlay carrying the supplied opaque tag."""
        return cls(tag)

    def isolations(self) -> dict[Key, RealmId]:
        """Return a snapshot retaining the existing realm identities."""
        with self._lock:
            return dict(self._isolations)

    def set_isolations(self, table: Mapping[str, RealmId]) -> None:
        """Replace the table for all contexts sharing this overlay."""
        replacement = {Key(key): realm for key, realm in table.items()}
        with self._lock:
            self._isolations = replacement

    def set_intercepts(self, table: Mapping[str, JsonValue]) -> None:
        """Replace metadata using JSON snapshots rather than caller-owned containers."""
        replacement = {Key(key): deepcopy(value) for key, value in table.items()}
        with self._lock:
            self._intercepts = replacement

    def isolate(self, key: str) -> RealmId | None:
        """Read this overlay's own realm override."""
        with self._lock:
            return self._isolations.get(Key(key))

    def intercept(self, key: str) -> tuple[bool, JsonValue]:
        """Distinguish a missing intercept from an explicitly stored JSON null."""
        with self._lock:
            name = Key(key)
            return name in self._intercepts, deepcopy(self._intercepts.get(name))


@dataclass(frozen=True)
class FiberScope:
    """A fiber's own node in the context derivation chain."""

    fiber: FiberId


@dataclass(frozen=True)
class IsolateScope:
    """One immutable realm override."""

    key: Key
    realm: RealmId


@dataclass(frozen=True)
class InterceptScope:
    """One immutable interception metadata entry."""

    key: Key
    value: JsonValue


ScopeKind: TypeAlias = FiberScope | IsolateScope | InterceptScope | Overlay


class Scope:
    """A derivation node whose parent can be changed without rebuilding descendants."""

    def __init__(self, parent: Scope | None, kind: ScopeKind) -> None:
        """Create a node with identity distinct from all otherwise equal nodes."""
        self._parent: Scope | None = parent
        self.kind: ScopeKind = kind
        self._lock = threading.Lock()

    @classmethod
    def root(cls) -> Scope:
        """Create the root fiber's own derivation node."""
        return cls(None, FiberScope(FiberId.ROOT))

    @classmethod
    def child(cls, parent: Scope, kind: ScopeKind) -> Scope:
        """Derive a new node from an existing scope."""
        return cls(parent, kind)

    def parent(self) -> Scope | None:
        """Read this node's current parent."""
        with self._lock:
            return self._parent

    def set_parent(self, parent: Scope) -> None:
        """Move this node and every descendant under a new derivation parent."""
        with self._lock:
            self._parent = parent

    def isolates(self, key: str) -> RealmId | None:
        """Return only this node's own realm override."""
        match self.kind:
            case IsolateScope(name, realm) if name == key:
                return realm
            case Overlay() as overlay:
                return overlay.isolate(key)
            case _:
                return None

    def own_realm(self, key: str) -> RealmId | None:
        """Find the nearest override along the current derivation chain."""
        for scope in self.ancestors():
            realm = scope.isolates(key)
            if realm is not None:
                return realm
        return None

    def intercepts(self, key: str) -> list[JsonValue]:
        """Return metadata snapshots outermost first, preserving explicit null entries."""
        values: list[JsonValue] = []
        for scope in self.ancestors():
            match scope.kind:
                case InterceptScope(name, value) if name == key:
                    values.append(deepcopy(value))
                case Overlay() as overlay:
                    found, value = overlay.intercept(key)
                    if found:
                        values.append(value)
        values.reverse()
        return values

    def fiber(self) -> FiberId | None:
        """Find the nearest fiber node, starting at this scope."""
        return next(
            (scope.kind.fiber for scope in self.ancestors() if isinstance(scope.kind, FiberScope)),
            None,
        )

    def tag(self) -> int | None:
        """Find the nearest tagged overlay, including the valid zero tag."""
        for scope in self.ancestors():
            if isinstance(scope.kind, Overlay) and scope.kind.tag is not None:
                return scope.kind.tag
        return None

    def is_within(self, ancestor: Scope) -> bool:
        """Test scope identity along the current parent chain, including this node."""
        return any(scope is ancestor for scope in self.ancestors())

    def isolated_keys(self) -> list[Key]:
        """Collect all isolated keys nearest first, retaining repeated overrides."""
        keys: list[Key] = []
        for scope in self.ancestors():
            match scope.kind:
                case IsolateScope(name, _):
                    keys.append(name)
                case Overlay() as overlay:
                    keys.extend(overlay.isolations())
        return keys

    def ancestors(self) -> Iterator[Scope]:
        """Iterate this node and its current ancestors nearest first."""
        current: Scope | None = self
        while current is not None:
            yield current
            current = current.parent()
