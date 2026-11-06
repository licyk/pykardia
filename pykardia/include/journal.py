"""Ordered runtime changes and file-winning three-way reconciliation."""

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass

from pykardia.core.value import JsonValue, json_equal
from pykardia.include.tree import Flat, detach, entry_mut, flatten, place
from pykardia.loader.backend import MISSING, Missing

Changes = dict[str, JsonValue | Missing]


@dataclass
class Remove:
    """Remove the entry regardless of its current options."""


@dataclass
class Upsert:
    """Merge fields, optionally moving or creating an entry."""

    created: bool
    parent: str | None | Missing
    position: int
    changes: Changes


Record = Remove | Upsert
Journal = dict[str, Record]


def diff_options(legacy: JsonValue, options: JsonValue) -> Changes:
    """Compute ordered non-ID changes, distinguishing deletion from JSON null."""
    old = legacy if isinstance(legacy, dict) else {}
    new = options if isinstance(options, dict) else {}
    return {
        key: deepcopy(new.get(key, MISSING))
        for key in dict.fromkeys([*old, *new])
        if key != "id" and not equal(old.get(key, MISSING), new.get(key, MISSING))
    }


def equal(left: JsonValue | Missing, right: JsonValue | Missing) -> bool:
    """Compare optional JSON values with source numeric and presence distinctions."""
    if isinstance(left, Missing) or isinstance(right, Missing):
        return left is right
    return json_equal(left, right)


def merge_records(older: Record | None, newer: Record) -> Record | None:
    """Fold one later record into an earlier record without aliasing the inputs."""
    if older is None:
        return deepcopy(newer)
    if isinstance(newer, Remove):
        return None if isinstance(older, Upsert) and older.created else Remove()
    if isinstance(older, Remove):
        return Upsert(False, newer.parent, newer.position, deepcopy(newer.changes))
    moved = not isinstance(newer.parent, Missing)
    return Upsert(
        older.created,
        newer.parent if moved else older.parent,
        newer.position if moved else older.position,
        deepcopy({**older.changes, **newer.changes}),
    )


def merge(journal: Journal, id: str, change: Record) -> None:
    """Append a merged record in its latest report order, dropping cancelled creations."""
    merged = merge_records(journal.pop(id, None), change)
    if merged is not None:
        journal[id] = merged


def record(
    journal: Journal,
    id: str,
    options: JsonValue,
    legacy: JsonValue,
    moved: bool,
    parent: str | None,
    position: int,
) -> None:
    """Record a tree mutation where None source objects indicate creation or removal."""
    if options is None:
        change: Record = Remove()
    elif legacy is None:
        changes: Changes = (
            {key: deepcopy(value) for key, value in options.items() if key != "id"}
            if isinstance(options, dict)
            else {}
        )
        change = Upsert(True, parent, position, changes)
    else:
        change = Upsert(
            False, parent if moved else MISSING, position, diff_options(legacy, options)
        )
    merge(journal, id, change)


def apply_changes(options: JsonValue, changes: Changes, filter: Callable[[str], bool]) -> None:
    """Apply admitted fields in place, with the missing sentinel deleting a field."""
    if not isinstance(options, dict):
        return
    for key, value in changes.items():
        if filter(key):
            if isinstance(value, Missing):
                options.pop(key, None)
            else:
                options[key] = deepcopy(value)


def apply_journal(
    data: list[JsonValue],
    journal: Journal,
    filter: Callable[[str, str | None], bool],
    warn: Callable[[str], None],
) -> list[JsonValue]:
    """Overlay admitted mutations on a snapshot, restoring moves with missing targets."""
    data = deepcopy(data)
    for id, change in journal.items():
        if not filter(id, None):
            continue
        if isinstance(change, Remove):
            detach(data, id)
            continue
        flat = flatten(data).get(id)
        if flat is None:
            options: JsonValue = {"id": id}
            apply_changes(options, change.changes, lambda key, id=id: filter(id, key))
            parent = None if isinstance(change.parent, Missing) else change.parent
            if not place(data, options, parent, change.position):
                warn(f"cannot place entry {id}: group {parent or ''} not found")
            continue
        apply_changes(entry_mut(data, id), change.changes, lambda key, id=id: filter(id, key))
        if not isinstance(change.parent, Missing) and change.parent != flat.parent:
            options = detach(data, id)
            if not place(data, options, change.parent, change.position):
                warn(f"cannot move entry {id}: group {change.parent or ''} not found")
                place(data, options, flat.parent, flat.position)
    return data


@dataclass
class Conflict:
    """A runtime mutation discarded because the file changed the same source location."""

    id: str
    reason: str


def reconcile(
    journal: Journal,
    base: dict[str, Flat],
    theirs: dict[str, Flat],
    file_owned: Callable[[str, str | None], bool],
) -> list[Conflict]:
    """Drop conflicting file-owned changes while preserving independent runtime edits."""
    conflicts: list[Conflict] = []
    for id in list(journal):
        if not file_owned(id, None):
            continue
        old, current = base.get(id), theirs.get(id)
        change = journal[id]
        if isinstance(change, Remove):
            if current is not None:
                if old is not None and json_equal(old.options, current.options):
                    continue
                reason = (
                    "modified in file, removed at runtime"
                    if old is not None
                    else "added in file, removed at runtime"
                )
                conflicts.append(Conflict(id, reason))
            del journal[id]
            continue
        if current is None:
            if old is not None:
                conflicts.append(Conflict(id, "removed in file, modified at runtime"))
                del journal[id]
            continue
        change.created = False
        old_options = old.options if old is not None and isinstance(old.options, dict) else {}
        their_options = current.options if isinstance(current.options, dict) else {}
        for key in list(change.changes):
            if not file_owned(id, key):
                continue
            before, after = old_options.get(key, MISSING), their_options.get(key, MISSING)
            if equal(before, after) or equal(after, change.changes[key]):
                continue
            conflicts.append(Conflict(id, f'key "{key}" modified both in file and at runtime'))
            del change.changes[key]
        if (
            not isinstance(change.parent, Missing)
            and old is not None
            and current.parent != old.parent
            and current.parent != change.parent
        ):
            conflicts.append(Conflict(id, "moved both in file and at runtime"))
            change.parent = MISSING
        moved = not isinstance(change.parent, Missing) and change.parent != current.parent
        if not change.changes and not moved:
            del journal[id]
    return conflicts
