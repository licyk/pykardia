"""Stable identities for file entries that omit an explicit ID."""

from copy import deepcopy
from dataclasses import dataclass

from pykardia.core.config import PathSegment
from pykardia.core.value import JsonValue, json_equal
from pykardia.include.patch import random_id
from pykardia.include.tree import group_list, id_of


@dataclass
class Anonymous:
    """An assigned ID and the unmodified source entry from the previous read."""

    id: str
    parent: str | None
    options: JsonValue


def assign(
    entries: list[JsonValue],
    parent: str | None,
    used: set[str],
    previous: list[Anonymous],
    named: list[Anonymous],
    store: set[str],
) -> None:
    """Reuse equal anonymous entries under the same parent before allocating fresh IDs."""
    for options in entries:
        if id_of(options) is None and isinstance(options, dict):
            reused = next(
                (
                    index
                    for index, item in enumerate(previous)
                    if item.parent == parent
                    and item.id not in used
                    and json_equal(item.options, options)
                ),
                None,
            )
            identifier = (
                previous.pop(reused).id
                if reused is not None
                else random_id(lambda id: id in used or id in store)
            )
            used.add(identifier)
            named.append(Anonymous(identifier, parent, deepcopy(options)))
            rest = dict(options)
            options.clear()
            options["id"] = identifier
            options.update(rest)
        members = group_list(options)
        if members is not None:
            assign(members, id_of(options), used, previous, named, store)


def collect_ids(entries: list[JsonValue], used: set[str]) -> None:
    """Collect explicit IDs in the source and all nested structural groups."""
    for options in entries:
        identifier = id_of(options)
        if identifier is not None:
            used.add(identifier)
        members = group_list(options)
        if members is not None:
            collect_ids(members, used)


def entry_path(indices: list[int]) -> list[PathSegment]:
    """Expand nested entry indices to a JSON path through group config fields."""
    path: list[PathSegment] = []
    for depth, index in enumerate(indices):
        if depth:
            path.append("config")
        path.append(index)
    return path
