# SPDX-License-Identifier: MIT OR Apache-2.0
"""The recursive JSON value type used at configuration and guest boundaries."""

import json
from dataclasses import fields, is_dataclass
from enum import Enum
from typing import TypeAlias, cast

JsonValue: TypeAlias = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]


def json_equal(left: JsonValue, right: JsonValue) -> bool:
    """Compare JSON values without Python's boolean/integer equality coercion."""
    if type(left) is not type(right):
        return False
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            json_equal(one, two) for one, two in zip(left, right, strict=True)
        )
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            json_equal(value, right[key]) for key, value in left.items()
        )
    return left == right


def is_json(value: object) -> bool:
    """Identify JSON data at provision boundaries, excluding arbitrary Python objects."""
    if value is None or type(value) in (str, int, float, bool):
        return True
    if isinstance(value, list):
        return all(is_json(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and is_json(item) for key, item in value.items())
    return False


def to_json(value: object) -> JsonValue:
    """Serialize an explicitly typed source or service view to an independent JSON value."""

    def default(item: object) -> object:
        """Adapt dataclass and enum values at the serialization boundary."""
        if is_dataclass(item) and not isinstance(item, type):
            return {
                member.metadata.get("alias", member.name): getattr(item, member.name)
                for member in fields(item)
            }
        if isinstance(item, Enum):
            return item.value
        raise TypeError(f"{type(item).__name__} is not JSON serializable")

    return cast(JsonValue, json.loads(json.dumps(value, default=default, allow_nan=False)))
