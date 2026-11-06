"""Loader tree records and synchronous source/identity transformations."""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypeAlias

from pykardia.core.context import Context
from pykardia.core.fiber import Fiber
from pykardia.core.id_table import IdTable
from pykardia.core.key import FiberId, RealmId
from pykardia.core.plugin import Inject
from pykardia.core.runtime import Completion
from pykardia.core.scope import Overlay
from pykardia.core.value import JsonValue
from pykardia.loader.backend import TreeBackend
from pykardia.loader.expr import evaluate_at, interpolate_at, truthy
from pykardia.loader.options import EntryOptions, sort_keys

if TYPE_CHECKING:
    from pykardia.loader.handles import EntryHandle

PatchHook: TypeAlias = Callable[["EntryHandle", Awaitable[None]], Awaitable[None]]


@dataclass
class TreeData:
    """A tree's context, local-ID store, backend and optional owning entry."""

    ctx: Context
    root: int
    backend: TreeBackend
    owner: int | None = None
    store: dict[str, int] = field(default_factory=dict)


@dataclass
class GroupData:
    """Ordered source options under one plugin's group context."""

    ctx: Context
    tree: int
    data: list[JsonValue] = field(default_factory=list)
    owner: int | None = None


@dataclass
class EntryData:
    """One registered entry, its contexts, fibers and shared initialization task."""

    parent: int
    ctx: Context
    overlay: Overlay
    intercept: Overlay
    options: JsonValue = field(default_factory=dict)
    outer: Fiber | None = None
    inner: Fiber | None = None
    subgroup: int | None = None
    subtree: int | None = None
    inject: Inject = field(default_factory=Inject)
    init: tuple[int, Completion[None]] | None = None
    disposing: bool = False
    verdict: bool | None = None
    local_realms: dict[str, RealmId] = field(default_factory=dict)

    def read(self) -> EntryOptions:
        """Read the already-validated typed view of this entry's source options."""
        return EntryOptions.read(self.options)

    def local_id(self) -> str:
        """Read the entry's ID relative to its owning tree."""
        return self.read().id

    def inner_live(self) -> bool:
        """Report whether the retained inner fiber has not retired."""
        return self.inner is not None and not self.inner.is_removed()


class LoaderState:
    """Mutable records accessed only inside loader critical sections."""

    def __init__(self) -> None:
        """Initialize empty tree stores, fiber indexes and shared-realm registrations."""
        self.entries: dict[int, EntryData] = {}
        self.groups: dict[int, GroupData] = {}
        self.trees: dict[int, TreeData] = {}
        self.inner_fibers: dict[FiberId, int] = {}
        self.outer_fibers: dict[FiberId, int] = {}
        self.global_realms: dict[str, dict[str, RealmId]] = {}
        self.patch_hooks: IdTable[PatchHook] = IdTable()

    def registered(self, entry: int) -> bool:
        """Check the current tree store instead of retained historical entry records."""
        data = self.entries.get(entry)
        return (
            data is not None
            and self.trees[self.groups[data.parent].tree].store.get(data.local_id()) == entry
        )

    def set_entry_options(self, entry: int, options: JsonValue) -> None:
        """Replace entry source and the corresponding group slot, then mirror group owners."""
        data = self.entries.get(entry)
        if data is None:
            return
        identifier = EntryOptions.read(options).id
        data.options = deepcopy(options)
        group = self.groups.get(data.parent)
        if group is not None:
            for index, item in enumerate(group.data):
                if isinstance(item, dict) and item.get("id") == identifier:
                    group.data[index] = deepcopy(options)
                    break
        self.sync_owner(data.parent)

    def sync_owner(self, group: int) -> None:
        """Mirror a same-tree group plugin's entry list into its owning entry config."""
        data = self.groups.get(group)
        if data is None or data.owner is None:
            return
        owner = self.entries.get(data.owner)
        if owner is None or owner.subgroup != group or self.groups[owner.parent].tree != data.tree:
            return
        options = deepcopy(owner.options)
        if isinstance(options, dict):
            options["config"] = deepcopy(data.data)
        self.set_entry_options(data.owner, options)

    def set_group_data(self, group: int, data: list[JsonValue]) -> None:
        """Replace a group's ordered source data and synchronize a same-tree owner."""
        target = self.groups.get(group)
        if target is not None:
            target.data = deepcopy(data)
        self.sync_owner(group)

    def composite_id(self, entry: int) -> str:
        """Join local IDs across nested owned trees using the source colon separator."""
        data = self.entries[entry]
        tree = self.trees[self.groups[data.parent].tree]
        if tree.owner in self.entries:
            assert tree.owner is not None
            return f"{self.composite_id(tree.owner)}:{data.local_id()}"
        return data.local_id()

    def ensure_id(self, tree: int, options: JsonValue) -> str:
        """Assign a noncolliding eight-digit lowercase hex ID only when missing or empty."""
        identifier = options.get("id") if isinstance(options, dict) else None
        if isinstance(identifier, str) and identifier:
            return identifier
        store = self.trees[tree].store
        while True:
            identifier = secrets.token_hex(4)
            if identifier not in store:
                break
        if isinstance(options, dict):
            options["id"] = identifier
        sort_keys(options)
        return identifier

    def resolve(self, tree: int, identifier: str) -> int:
        """Resolve nested tree IDs or raise the source diagnostic for any missing segment."""
        parts = identifier.split(":")
        for part in parts[:-1]:
            entry = self.trees[tree].store.get(part)
            subtree = self.entries[entry].subtree if entry is not None else None
            if subtree is None:
                raise ValueError(f"cannot resolve entry {identifier}")
            tree = subtree
        entry = self.trees[tree].store.get(parts[-1])
        if entry is None:
            raise ValueError(f"cannot resolve entry {identifier}")
        return entry

    def tree_entries(self, tree: int) -> list[int]:
        """Enumerate creation-ordered entries and nested trees depth first."""
        result: list[int] = []
        for identity in sorted(self.trees[tree].store.values()):
            result.append(identity)
            subtree = self.entries[identity].subtree
            if subtree is not None and subtree in self.trees:
                result.extend(self.tree_entries(subtree))
        return result

    def members(self, entry: int) -> list[int]:
        """Collect an entry and its group descendants within the same owning tree."""
        result = [entry]
        tree = self.groups[self.entries[entry].parent].tree
        group = self.entries[entry].subgroup
        if group is None or self.groups[group].tree != tree:
            return result
        for options in self.groups[group].data:
            member = self.trees[tree].store.get(EntryOptions.read(options).id)
            if member is not None and self.entries[member].parent == group:
                result.extend(self.members(member))
        return result

    def move_entry(self, entry: int, source: int, target: int, position: int | None) -> None:
        """Move source data and transfer same-tree group descendants across tree stores."""
        source_tree, target_tree = self.groups[source].tree, self.groups[target].tree
        moved = [] if source_tree == target_tree else self.members(entry)
        for member in moved:
            identifier = self.entries[member].local_id()
            if identifier in self.trees[target_tree].store:
                raise ValueError(f"entry {identifier} already exists in the target tree")
        options = deepcopy(self.entries[entry].options)
        identifier = EntryOptions.read(options).id
        self.set_group_data(
            source,
            [item for item in self.groups[source].data if EntryOptions.read(item).id != identifier],
        )
        data = deepcopy(self.groups[target].data)
        data.insert(len(data) if position is None else min(position, len(data)), options)
        self.set_group_data(target, data)
        self.entries[entry].parent = target
        for member in moved:
            identifier = self.entries[member].local_id()
            self.trees[source_tree].store.pop(identifier, None)
            self.trees[target_tree].store[identifier] = member
            group = self.entries[member].subgroup
            if group is not None and self.groups[group].tree == source_tree:
                self.groups[group].tree = target_tree

    def new_entry(self, identity: int, loader_ctx: Context, parent: int) -> None:
        """Create tagged reparentable isolation and interception overlays for an entry."""
        overlay = Overlay.tagged(identity)
        self.entries[identity] = EntryData(
            parent, loader_ctx.extend_with(overlay), overlay, Overlay.tagged(identity)
        )

    def admit(self, entry: int, ctx: Context) -> tuple[bool, list[Exception]]:
        """Evaluate disabled before intercept, propagating a group's verdict to children only."""
        failures: list[Exception] = []
        data = self.entries[entry]
        disabled = data.read().disabled
        try:
            data.verdict = (
                truthy(evaluate_at(ctx, disabled, ["disabled"]))
                if isinstance(disabled, str)
                else bool(disabled)
            )
        except Exception as error:
            data.verdict = True
            failures.append(error)
        if self.is_disabled(entry):
            return False, failures
        try:
            table = interpolate_at(ctx, data.read().intercept, ["intercept"])
            if isinstance(table, dict):
                data.intercept.set_intercepts(table)
            return True, failures
        except Exception as error:
            data.verdict = True
            failures.append(error)
            return False, failures

    def is_disabled(self, entry: int) -> bool:
        """Read cached verdicts without re-evaluating expressions or disabling structural groups."""
        data = self.entries.get(entry)
        if data is None:
            return True
        if data.read().group:
            return False
        return data.verdict is not False or self.parent_disabled(entry)

    def parent_disabled(self, entry: int) -> bool:
        """Walk owner verdicts through both group and nested-tree ancestry."""
        group = self.entries[entry].parent
        while group in self.groups and (owner := self.groups[group].owner) is not None:
            data = self.entries.get(owner)
            if data is None:
                return False
            if data.verdict is not False:
                return True
            group = data.parent
        return False

    def blocked(self, entry: int) -> bool:
        """Block literal disables and disabled ancestors before importing a non-group entry."""
        data = self.entries.get(entry)
        if data is None:
            return True
        options = data.read()
        if options.group:
            return False
        if options.disabled is True:
            data.verdict = True
            return True
        return self.parent_disabled(entry)

    def realm(self, entry: int, name: str, label: str | None) -> RealmId:
        """Reuse a private entry realm or a globally shared service/label realm."""
        table = (
            self.entries[entry].local_realms
            if label is None
            else self.global_realms.setdefault(label, {})
        )
        if name not in table:
            table[name] = RealmId.new()
        return table[name]

    def collect_realms(self, entry: int, legacy: JsonValue, active: bool) -> None:
        """Release shared labels no registered entry still references."""
        data = self.entries.get(entry)
        current = data.read().isolate if data is not None else []
        for name, label in EntryOptions.read(legacy).isolate:
            if (
                label is None
                or (active and (name, label) in current)
                or label not in self.global_realms
            ):
                continue
            if any(
                self.registered(identity) and (name, label) in data.read().isolate
                for identity, data in self.entries.items()
            ):
                continue
            self.global_realms[label].pop(name, None)
            if not self.global_realms[label]:
                del self.global_realms[label]

    def chain(self, group: int, identifier: str) -> list[tuple[TreeBackend, str]]:
        """Collect backend/local-ID frames from the innermost entry to its owners."""
        chain: list[tuple[TreeBackend, str]] = []
        while group in self.groups:
            data = self.groups[group]
            chain.append((self.trees[data.tree].backend, identifier))
            owner = self.entries.get(data.owner) if data.owner is not None else None
            if owner is None:
                break
            identifier, group = owner.local_id(), owner.parent
        return chain
