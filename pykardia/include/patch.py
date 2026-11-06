"""Overlay include patches and route runtime changes back to their actual owners."""

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from secrets import token_hex
from typing import Literal, cast

from pykardia.core.value import JsonValue
from pykardia.include.journal import Journal, Remove, Upsert, apply_changes
from pykardia.include.tree import detach, entry_mut, flatten, get_mut, group_list, id_of, place
from pykardia.loader.backend import Missing


class FileOwner(Enum):
    """The entry or field is supplied directly by the included file."""

    FILE = "file"


@dataclass(frozen=True)
class PatchOwner:
    """A field overridden by a patch at the given index."""

    index: int


@dataclass(frozen=True)
class EmbeddedOwner:
    """An entry supplied in a patch's insert or config member list."""

    index: int
    key: Literal["insert", "config"]


Owner = FileOwner | PatchOwner | EmbeddedOwner
FILE = FileOwner.FILE


@dataclass
class PatchIndex:
    """Track entry ownership separately from individual field overrides."""

    owners: dict[str, Owner] = field(default_factory=dict)
    keys: dict[str, dict[str, int]] = field(default_factory=dict)

    def entry(self, id: str | None) -> Owner:
        """Return the mounted entry owner, defaulting to the file."""
        return self.owners.get(id, FILE) if id is not None else FILE

    def key(self, id: str, key: str) -> Owner:
        """Prefer the field's last override over the entry's original owner."""
        position = self.keys.get(id, {}).get(key)
        return PatchOwner(position) if position is not None else self.entry(id)

    def file_owned(self, id: str, key: str | None) -> bool:
        """Report whether the file expresses this entry or its requested field."""
        return (self.entry(id) if key is None else self.key(id, key)) == FILE


def random_id(used: Callable[[str], bool]) -> str:
    """Generate an eight-digit hexadecimal ID absent from the caller's ID space."""
    while used(identifier := token_hex(4)):
        pass
    return identifier


def ensure_ids(entries: list[JsonValue], used: Callable[[str], bool]) -> None:
    """Assign missing IDs before existing fields, recursively through group members."""
    for entry in entries:
        if id_of(entry) is None and isinstance(entry, dict):
            identifier = random_id(used)
            rest = dict(entry)
            entry.clear()
            entry["id"] = identifier
            entry.update(rest)
        members = group_list(entry)
        if members is not None:
            ensure_ids(members, used)


def ensure_insert_ids(patches: list[JsonValue], used: Callable[[str], bool]) -> None:
    """Assign stable source IDs inside insert lists before their first application."""
    for patch in patches:
        entries = patch.get("insert") if isinstance(patch, dict) else None
        if isinstance(entries, list):
            ensure_ids(entries, used)


@dataclass
class Patched:
    """The resulting tree snapshot and its field ownership index."""

    data: list[JsonValue]
    index: PatchIndex


def apply_patches(
    data: list[JsonValue], patches: list[JsonValue], warn: Callable[[str], None]
) -> Patched:
    """Apply ordered patches, retaining generated group IDs on the patch source itself."""
    data = deepcopy(data)
    index = PatchIndex()
    paths: dict[str, list[int]] = {}
    mount(data, [], FILE, index, paths)
    for position, raw in enumerate(patches):
        if not isinstance(raw, dict):
            continue
        identifier = raw.get("id")
        id = identifier if isinstance(identifier, str) else None
        target = paths.get(id) if id is not None else None
        if "insert" in raw:
            source = raw["insert"]
            insert = deepcopy(source) if isinstance(source, list) else []
            if id is not None:
                if target is None:
                    warn(f"patch insert: entry {id} not found")
                    continue
                group = get_mut(data, target)
                if not isinstance(group, dict) or group.get("group") is not True:
                    warn(f"patch insert: entry {id} is not a group")
                    continue
                members = group.get("config")
                if not isinstance(members, list):
                    members = []
                    group["config"] = members
                parent = target
            else:
                members, parent = data, []
            start = len(members)
            members.extend(insert)
            mount(insert, parent, EmbeddedOwner(position, "insert"), index, paths, start)
            continue
        if id is None:
            warn("patch: id is required for non-insert patches")
            continue
        if target is None:
            warn(f"patch: entry {id} not found")
            continue
        options = cast(dict[str, JsonValue], get_mut(data, target))
        name = raw.get("name")
        if isinstance(name, str) and options.get("name") != name:
            actual = options.get("name")
            actual = actual if isinstance(actual, str) else ""
            warn(f"patch: name mismatch for {id} (expected {actual}, got {name}), skipping")
            continue
        members = group_list(options)
        if "config" in raw and members is not None:
            for member in flatten(members):
                index.owners.pop(member, None)
                index.keys.pop(member, None)
                paths.pop(member, None)
            config = raw["config"]
            if isinstance(config, list):
                ensure_ids(config, paths.__contains__)
        overridden = index.keys.setdefault(id, {})
        for key, value in raw.items():
            if key not in ("id", "name", "insert"):
                options[key] = deepcopy(value)
                overridden[key] = position
        members = group_list(options)
        if "config" in raw and members is not None:
            mount(members, target, EmbeddedOwner(position, "config"), index, paths)
    return Patched(data, index)


def mount(
    entries: list[JsonValue],
    parent: list[int],
    owner: Owner,
    index: PatchIndex,
    paths: dict[str, list[int]],
    start: int = 0,
) -> None:
    """Mount each entry at its actual index, replacing obsolete per-field owners."""
    for position, options in enumerate(entries, start):
        path = [*parent, position]
        identifier = id_of(options)
        if identifier is not None:
            index.owners[identifier] = owner
            index.keys.pop(identifier, None)
            paths[identifier] = path
        members = group_list(options)
        if members is not None:
            mount(members, path, owner, index, paths)


def route_journal(
    journal: Journal,
    data: list[JsonValue],
    patches: list[JsonValue],
    warn: Callable[[str], None],
) -> bool:
    """Apply runtime records to their file or patch source and report patch changes."""
    index = apply_patches(data, patches, lambda text: None).index
    patched = False
    for id, change in journal.items():
        owner = index.entry(id)
        embedded = isinstance(owner, EmbeddedOwner)
        raw_base = (
            cast(dict[str, JsonValue], patches[owner.index]).get("id")
            if isinstance(owner, EmbeddedOwner)
            else None
        )
        base = raw_base if isinstance(raw_base, str) else None
        if isinstance(change, Remove):
            detach(container(data, patches, owner), id)
            patched |= embedded
            index = apply_patches(data, patches, lambda text: None).index
            continue
        current = flatten(container(data, patches, owner)).get(id)
        if current is None:
            options: JsonValue = {"id": id}
            apply_changes(options, change.changes, lambda key: True)
            parent = None if isinstance(change.parent, Missing) else change.parent
            patched |= place_at(data, patches, index, options, parent, change.position, warn)
            index = apply_patches(data, patches, lambda text: None).index
            continue
        effective = current.parent if current.parent is not None else base
        if not isinstance(change.parent, Missing) and effective != change.parent:
            options = detach(container(data, patches, owner), id)
            patched |= embedded
            apply_changes(options, change.changes, lambda key: True)
            patched |= place_at(data, patches, index, options, change.parent, change.position, warn)
            index = apply_patches(data, patches, lambda text: None).index
            continue
        options = entry_mut(container(data, patches, owner), id)
        patch_changes: dict[int, dict[str, JsonValue]] = {}

        def filter_key(
            key: str,
            id: str = id,
            change: Upsert = change,
            index: PatchIndex = index,
            patch_changes: dict[int, dict[str, JsonValue]] = patch_changes,
        ) -> bool:
            """Collect override changes while allowing original-owner fields through."""
            target_owner = index.key(id, key)
            if isinstance(target_owner, PatchOwner):
                value = change.changes[key]
                patch_changes.setdefault(target_owner.index, {})[key] = (
                    None if isinstance(value, Missing) else deepcopy(value)
                )
                return False
            return True

        apply_changes(options, change.changes, filter_key)
        patched |= embedded
        for position, changes in patch_changes.items():
            patch = patches[position]
            if isinstance(patch, dict):
                for key, value in changes.items():
                    if value is None:
                        patch.pop(key, None)
                    else:
                        patch[key] = value
            patched = True
    return patched


def container(data: list[JsonValue], patches: list[JsonValue], owner: Owner) -> list[JsonValue]:
    """Return the mutable source list containing an entry owned by this location."""
    if isinstance(owner, EmbeddedOwner):
        patch = cast(dict[str, JsonValue], patches[owner.index])
        entries = patch.get(owner.key)
        if not isinstance(entries, list):
            entries = []
            patch[owner.key] = entries
        return entries
    return data


def place_at(
    data: list[JsonValue],
    patches: list[JsonValue],
    index: PatchIndex,
    options: JsonValue,
    parent: str | None,
    position: int,
    warn: Callable[[str], None],
) -> bool:
    """Place an entry in the parent config's actual owner, returning whether a patch changed."""
    if parent is not None:
        owner = index.key(parent, "config")
        if isinstance(owner, PatchOwner):
            patch = patches[owner.index]
            members = patch.get("config") if isinstance(patch, dict) else None
            if isinstance(members, list):
                members.insert(min(position, len(members)), options)
                return True
        elif isinstance(owner, EmbeddedOwner):
            if place(container(data, patches, owner), deepcopy(options), parent, position):
                return True
    if not place(data, options, parent, position):
        warn(f"cannot place entry: group {parent or ''} not found")
    return False
