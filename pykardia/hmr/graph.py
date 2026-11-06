"""Host-owned module cache, static dependencies and synchronous source evaluation."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Iterable
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from threading import RLock
from typing import TypeAlias

from pykardia.core.plugin import PluginDef
from pykardia.core.value import JsonValue
from pykardia.loader.catalog import Resolver

ModuleId: TypeAlias = str


class ExportKind(Enum):
    """Distinguish an empty export, plugin definition and JSON value including null."""

    NONE = "none"
    PLUGIN = "plugin"
    VALUE = "value"


@dataclass(frozen=True)
class Exports:
    """One exported module payload, with stable plugin identity and copied JSON data."""

    kind: ExportKind
    plugin_def: PluginDef | None = None
    json_value: JsonValue = None

    @classmethod
    def none(cls) -> Exports:
        """Represent a successfully evaluated module that exports nothing."""
        return cls(ExportKind.NONE)

    @classmethod
    def plugin(cls, definition: PluginDef) -> Exports:
        """Export a plugin definition without changing its identity."""
        return cls(ExportKind.PLUGIN, plugin_def=definition)

    @classmethod
    def value(cls, value: JsonValue) -> Exports:
        """Export an independently owned JSON snapshot."""
        return cls(ExportKind.VALUE, json_value=deepcopy(value))

    def clone(self) -> Exports:
        """Copy JSON ownership while retaining the same immutable plugin definition."""
        return Exports(self.kind, self.plugin_def, deepcopy(self.json_value))


@dataclass(frozen=True)
class Imports:
    """Only the module's declared static dependencies are visible during evaluation."""

    _graph: ModuleGraph
    _deps: list[ModuleId]

    def get(self, id: str) -> Exports | None:
        """Return a declared dependency's export, or None before it has evaluated."""
        return self._graph.exports(id) if id in self._deps else None

    def value(self, id: str) -> JsonValue:
        """Return a declared JSON export, or None for an absent or non-value export."""
        exports = self.get(id)
        return (
            exports.json_value if exports is not None and exports.kind is ExportKind.VALUE else None
        )


@dataclass(frozen=True)
class ModuleCode:
    """Static module dependencies and the synchronous evaluation entry point."""

    deps: list[ModuleId]
    evaluate: Callable[[Imports], Exports]

    @classmethod
    def new(cls, deps: list[ModuleId], evaluate: Callable[[Imports], Exports]) -> ModuleCode:
        """Capture declared dependency order and the module's evaluation callback."""
        return cls(list(deps), evaluate)


class ModuleSource(ABC):
    """Load current module code from a host-provided source."""

    @abstractmethod
    def load(self, id: str) -> ModuleCode:
        """Return current code or raise its load error without caching failures."""


@dataclass
class Record:
    """One evaluated module's ordered dependency edges and owned exports."""

    deps: list[ModuleId]
    exports: Exports


@dataclass
class Backup:
    """An evicted record that can be consumed once by transaction restoration."""

    record: Record | None


class ModuleGraph(Resolver):
    """Cache successfully evaluated modules and expose plugin exports to the loader."""

    def __init__(self, source: ModuleSource) -> None:
        """Retain the source with empty insertion-ordered records and host marks."""
        self._source = source
        self._records: dict[ModuleId, Record] = {}
        self._hosts: set[ModuleId] = set()
        self._lock = RLock()

    def import_(self, id: str) -> Exports:
        """Import one module and its static closure, reusing successful cache records."""
        return self._import_with(id, [])

    def mark_host(self, ids: Iterable[ModuleId]) -> None:
        """Mark modules whose code can change only when the host process restarts."""
        with self._lock:
            self._hosts.update(ids)

    def is_host(self, id: str) -> bool:
        """Return whether the module is marked as host-owned."""
        with self._lock:
            return id in self._hosts

    def is_loaded(self, id: str) -> bool:
        """Return whether evaluation has successfully created a cache record."""
        with self._lock:
            return id in self._records

    def loaded(self) -> list[ModuleId]:
        """Snapshot loaded module IDs in cache insertion order."""
        with self._lock:
            return list(self._records)

    def edges(self, id: str) -> list[ModuleId]:
        """Snapshot a module's declared dependencies, retaining duplicates and order."""
        with self._lock:
            record = self._records.get(id)
            return list(record.deps) if record is not None else []

    def exports(self, id: str) -> Exports | None:
        """Copy a cached export without evaluating an absent module."""
        with self._lock:
            record = self._records.get(id)
            return record.exports.clone() if record is not None else None

    def resolve(self, name: str) -> Awaitable[PluginDef]:
        """Import immediately and return the captured plugin result as an awaitable."""
        error: Exception | None = None
        definition: PluginDef | None = None
        try:
            exports = self.import_(name)
            if exports.kind is not ExportKind.PLUGIN:
                raise ValueError(f"module {name} does not export a plugin")
            definition = exports.plugin_def
        except Exception as caught:
            error = caught

        async def ready() -> PluginDef:
            """Observe the result captured when resolve was called."""
            if error is not None:
                raise error
            assert definition is not None
            return definition

        return ready()

    def _import_with(self, id: str, loading: list[ModuleId]) -> Exports:
        """Evaluate dependencies first; cyclic imports see an unevaluated export."""
        cached = self.exports(id)
        if cached is not None:
            return cached
        if id in loading:
            return Exports.none()
        loading.append(id)
        try:
            code = self._source.load(id)
            for dep in code.deps:
                self._import_with(dep, loading)
            exports = code.evaluate(Imports(self, code.deps))
            with self._lock:
                self._records[id] = Record(list(code.deps), exports.clone())
            return exports
        finally:
            loading.pop()

    def _host_ids(self) -> set[ModuleId]:
        """Snapshot host marks for one transaction."""
        with self._lock:
            return set(self._hosts)

    def _evict(self, id: str) -> Backup:
        """Remove a cache slot while retaining its exact record for a possible abort."""
        with self._lock:
            return Backup(self._records.pop(id, None))

    def _restore(self, id: str, backup: Backup) -> None:
        """Replace any new version with the saved record at the end of cache order."""
        with self._lock:
            self._records.pop(id, None)
            if backup.record is not None:
                self._records[id], backup.record = backup.record, None
