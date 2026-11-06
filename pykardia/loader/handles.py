"""Public entry, group and tree handles retaining loader identity."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pykardia.core.config import PathSegment
from pykardia.core.context import Context
from pykardia.core.fiber import Fiber
from pykardia.core.value import JsonValue
from pykardia.loader.backend import MISSING, EntryChange, Location, Missing
from pykardia.loader.options import (
    EntryOptions,
    EntryValidationError,
    merge,
    sort_keys,
    validate_options,
)

if TYPE_CHECKING:
    from pykardia.loader.loader import Loader


@dataclass(frozen=True, repr=False)
class EntryHandle:
    """Identity-preserving access to a loader entry and its current fibers."""

    _loader: Loader
    _id: int

    def __repr__(self) -> str:
        """Display the internal entry identity without traversing the loader state."""
        return f"EntryHandle({self._id})"

    async def init(self) -> None:
        """Start an absent entry or join its already pending initialization."""
        await self._loader.init(self._id, None)

    def id(self) -> str:
        """Return the composite ID across nested trees."""
        with self._loader._lock:
            return self._loader._state.composite_id(self._id)

    def options(self) -> JsonValue:
        """Return a source snapshot, or null if the entry record no longer exists."""
        with self._loader._lock:
            data = self._loader._state.entries.get(self._id)
            return deepcopy(data.options) if data is not None else None

    def inner_fiber(self) -> Fiber | None:
        """Return the plugin fiber, including a retained removed fiber."""
        with self._loader._lock:
            data = self._loader._state.entries.get(self._id)
            return data.inner if data is not None else None

    def outer_fiber(self) -> Fiber | None:
        """Return the current entry-injection fiber when one exists."""
        with self._loader._lock:
            data = self._loader._state.entries.get(self._id)
            return data.outer if data is not None else None

    def is_disabled(self) -> bool:
        """Read the cached local and ancestor verdicts without evaluating expressions."""
        with self._loader._lock:
            return self._loader._state.is_disabled(self._id)

    def subgroup(self) -> GroupHandle | None:
        """Return the group created by this entry's plugin, when present."""
        with self._loader._lock:
            data = self._loader._state.entries.get(self._id)
            identity = data.subgroup if data is not None else None
        return GroupHandle(self._loader, identity) if identity is not None else None

    def subtree(self) -> TreeHandle | None:
        """Return the nested tree created by this entry's plugin, when present."""
        with self._loader._lock:
            data = self._loader._state.entries.get(self._id)
            identity = data.subtree if data is not None else None
        return TreeHandle(self._loader, identity) if identity is not None else None

    def parent(self) -> GroupHandle:
        """Return the entry's current group after any moves."""
        with self._loader._lock:
            return GroupHandle(self._loader, self._loader._state.entries[self._id].parent)


@dataclass(frozen=True, repr=False)
class GroupHandle:
    """One ordered entry group, addressed independently of its owning fiber."""

    _loader: Loader
    _id: int

    def __repr__(self) -> str:
        """Display the internal group identity."""
        return f"GroupHandle({self._id})"

    def owner(self) -> EntryHandle | None:
        """Return the entry owning this group, or None for the loader's root group."""
        with self._loader._lock:
            data = self._loader._state.groups.get(self._id)
            identity = data.owner if data is not None else None
        return EntryHandle(self._loader, identity) if identity is not None else None

    def data(self) -> list[JsonValue]:
        """Return an independent snapshot of source options in group order."""
        with self._loader._lock:
            data = self._loader._state.groups.get(self._id)
            return deepcopy(data.data) if data is not None else []

    def tree(self) -> TreeHandle:
        """Return this group's current owning tree."""
        with self._loader._lock:
            return TreeHandle(self._loader, self._loader._state.groups[self._id].tree)

    def context(self) -> Context:
        """Return the group context under which entry contexts are attached."""
        with self._loader._lock:
            return self._loader._state.groups[self._id].ctx

    async def update(self, config: list[JsonValue]) -> None:
        """Reconcile source entries concurrently while isolating individual entry failures."""
        await self._loader.group_update(self._id, deepcopy(config))

    def stop(self) -> None:
        """Retire all group entries while preserving their written source data."""
        self._loader.group_stop(self._id)


@dataclass(frozen=True, repr=False)
class TreeHandle:
    """A local entry namespace and the persistence boundary for its groups."""

    _loader: Loader
    _id: int

    def __repr__(self) -> str:
        """Display the internal tree identity."""
        return f"TreeHandle({self._id})"

    def base_dir(self) -> Path:
        """Resolve an explicit backend base, containing tree base, or working directory."""
        with self._loader._lock:
            data = self._loader._state.trees[self._id]
            backend, owner = data.backend, data.owner
            parent = (
                EntryHandle(self._loader, owner)
                if owner in self._loader._state.entries and owner is not None
                else None
            )
        base = backend.base_dir()
        if base is not None:
            return base
        return parent.parent().tree().base_dir() if parent is not None else Path.cwd()

    def handle(self) -> object | None:
        """Return the backend's owner object if its tree still exists."""
        with self._loader._lock:
            data = self._loader._state.trees.get(self._id)
            backend = data.backend if data is not None else None
        return backend.handle() if backend is not None else None

    def root(self) -> GroupHandle:
        """Return this tree's root group."""
        with self._loader._lock:
            return GroupHandle(self._loader, self._loader._state.trees[self._id].root)

    def context(self) -> Context:
        """Return the tree context inherited by its root group."""
        with self._loader._lock:
            return self._loader._state.trees[self._id].ctx

    def entries(self) -> list[EntryHandle]:
        """Enumerate registered entries and owned trees in source creation order."""
        with self._loader._lock:
            return [
                EntryHandle(self._loader, identity)
                for identity in self._loader._state.tree_entries(self._id)
            ]

    def resolve(self, id: str) -> EntryHandle:
        """Resolve an ID in this tree, descending through colon-separated owned trees."""
        with self._loader._lock:
            return EntryHandle(self._loader, self._loader._state.resolve(self._id, id))

    def locate(
        self, id: str, path: list[PathSegment], value: JsonValue | Missing = MISSING
    ) -> Location | None:
        """Delegate source-value location to this tree's backend."""
        with self._loader._lock:
            data = self._loader._state.trees.get(self._id)
        return data.backend.locate(id, path, value) if data is not None else None

    def locate_expr(
        self, id: str, path: list[PathSegment], expr: str, offset: int
    ) -> Location | None:
        """Delegate expression character mapping to this tree's backend."""
        with self._loader._lock:
            data = self._loader._state.trees.get(self._id)
        return data.backend.locate_expr(id, path, expr, offset) if data is not None else None

    async def resolve_group(self, parent: str | None) -> int:
        """Wait for a loading parent plugin to create its group when necessary."""
        if parent is None:
            return self.root()._id
        entry = self.resolve(parent)
        group = entry.subgroup()
        if group is not None:
            return group._id
        inner = entry.inner_fiber()
        if inner is not None:
            try:
                await inner.settled()
            except Exception:
                pass
        group = entry.subgroup()
        if group is None:
            raise ValueError(f"entry {parent} is not a group")
        return group._id

    async def create(
        self, options: JsonValue, parent: str | None = None, position: int | None = None
    ) -> str:
        """Validate a complete runtime entry before inserting, committing and starting it."""
        group = await self.resolve_group(parent)
        issues = validate_options(options, True)
        if issues:
            raise EntryValidationError(issues)
        options = deepcopy(options)
        with self._loader._lock:
            state = self._loader._state
            tree = state.groups[group].tree
            identity = state.ensure_id(tree, options)
            data = deepcopy(state.groups[group].data)
            data.insert(
                len(data) if position is None else min(position, len(data)), deepcopy(options)
            )
            state.set_group_data(group, data)
        self._loader.commit(
            tree, EntryChange(identity, GroupHandle(self._loader, group), None, options, None)
        )
        return await self._loader.group_create(group, options)

    def remove(self, id: str) -> None:
        """Unregister before starting disposal, then commit one removal to the source tree."""
        entry = self.resolve(id)
        group, legacy = entry.parent(), entry.options()
        local = EntryOptions.read(legacy).id
        self._loader.group_remove(group._id, local, False)
        self._loader.commit(group.tree()._id, EntryChange(local, group, None, None, legacy))

    async def update(
        self,
        id: str,
        patch: JsonValue,
        parent: str | None | Missing = MISSING,
        position: int | None = None,
    ) -> None:
        """Patch an entry; omitted parent keeps its group, while None moves it to tree root."""
        entry = self.resolve(id)
        source, legacy = entry.parent(), entry.options()
        options = deepcopy(legacy)
        merge(options, patch if isinstance(patch, dict) else {})
        sort_keys(options)
        issues = validate_options(options, True)
        if issues:
            raise EntryValidationError(issues)
        target = await self.resolve_group(parent) if not isinstance(parent, Missing) else None
        source_tree = source.tree()._id
        with self._loader._lock:
            if target is not None:
                self._loader._state.move_entry(entry._id, source._id, target, position)
            self._loader._state.set_entry_options(entry._id, options)
        group = entry.parent()
        target_tree = group.tree()._id
        local = EntryOptions.read(legacy).id
        if target_tree != source_tree:
            self._loader.commit(source_tree, EntryChange(local, source, None, None, legacy))
            self._loader.commit(target_tree, EntryChange(local, group, None, options, None))
        else:
            self._loader.commit(
                target_tree,
                EntryChange(local, group, source if group != source else None, options, legacy),
            )
        await self._loader.entry_update(entry._id, legacy, True)
