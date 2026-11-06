"""Navigation and mutation of nested JSON entry lists."""

from copy import deepcopy
from dataclasses import dataclass

from pykardia.core.value import JsonValue


def id_of(options: JsonValue) -> str | None:
    """Read a nonempty entry ID without rejecting incomplete source objects."""
    value = options.get("id") if isinstance(options, dict) else None
    return value if isinstance(value, str) and value else None


def group_list(options: JsonValue) -> list[JsonValue] | None:
    """Return the actual member list only for a structural group with array config."""
    value = (
        options.get("config")
        if isinstance(options, dict) and options.get("group") is True
        else None
    )
    return value if isinstance(value, list) else None


@dataclass
class Flat:
    """An entry snapshot with its containing group and index in that group."""

    parent: str | None
    position: int
    options: JsonValue


def flatten(data: list[JsonValue]) -> dict[str, Flat]:
    """Index nested entry snapshots by ID in first encounter order."""
    result: dict[str, Flat] = {}

    def walk(entries: list[JsonValue], parent: str | None) -> None:
        """Retain source positions while traversing structural groups."""
        for position, options in enumerate(entries):
            identifier = id_of(options)
            if identifier is not None:
                result[identifier] = Flat(parent, position, deepcopy(options))
            members = group_list(options)
            if members is not None:
                walk(members, identifier)

    walk(data, None)
    return result


def find(data: list[JsonValue], id: str) -> list[int] | None:
    """Return the first entry's index path through structural group config lists."""
    for index, options in enumerate(data):
        if id_of(options) == id:
            return [index]
        members = group_list(options)
        if members is not None:
            path = find(members, id)
            if path is not None:
                return [index, *path]
    return None


def get_mut(data: list[JsonValue], path: list[int]) -> JsonValue:
    """Read the actual entry at an index path, or None for an absent path."""
    if not path or not 0 <= path[0] < len(data):
        return None
    options = data[path[0]]
    if len(path) == 1:
        return options
    members = group_list(options)
    return get_mut(members, path[1:]) if members is not None else None


def entry_mut(data: list[JsonValue], id: str) -> JsonValue:
    """Return a mutable entry object by ID, or None if absent."""
    path = find(data, id)
    return get_mut(data, path) if path is not None else None


def detach(data: list[JsonValue], id: str) -> JsonValue:
    """Remove an entry from its containing group and return its source object."""
    path = find(data, id)
    if path is None:
        return None
    members = data if len(path) == 1 else group_list(get_mut(data, path[:-1]))
    assert members is not None
    return members.pop(path[-1])


def place(data: list[JsonValue], options: JsonValue, parent: str | None, position: int) -> bool:
    """Insert at the clamped position, creating a group's missing config list as needed."""
    members = data
    if parent is not None:
        group = entry_mut(data, parent)
        if not isinstance(group, dict) or group.get("group") is not True:
            return False
        config = group.get("config")
        if not isinstance(config, list):
            config = []
            group["config"] = config
        members = config
    members.insert(min(position, len(members)), options)
    return True
