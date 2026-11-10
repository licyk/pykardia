# SPDX-License-Identifier: MIT OR Apache-2.0
"""Typed configuration decoding and reference-compatible issue paths."""

from __future__ import annotations

import json
from collections.abc import Iterable
from copy import deepcopy
from dataclasses import MISSING, dataclass, field, fields, is_dataclass
from enum import Enum, IntEnum
from types import NoneType, UnionType
from typing import (
    Annotated,
    ForwardRef,
    TypeAlias,
    TypeVar,
    Union,
    cast,
    get_args,
    get_origin,
    get_type_hints,
)

from typing_extensions import TypeForm

from pykardia.core.value import JsonValue

T = TypeVar("T")
ErasedConfig: TypeAlias = object
NoConfig = NoneType
PathSegment: TypeAlias = str | int


@dataclass
class Issue:
    """One validation problem with a path of object keys and array indices."""

    message: str
    path: list[PathSegment] = field(default_factory=list)

    def at(self, path: Iterable[PathSegment]) -> Issue:
        """Return this issue with its path set to the supplied segments."""
        self.path = list(path)
        return self

    def format_path(self) -> str:
        """Join path segments with dots, preserving the reference's diagnostic spelling."""
        return ".".join(map(str, self.path))


class ValidationError(Exception):
    """A rejected configuration containing one or more located issues."""

    def __init__(self, issues: list[Issue]) -> None:
        """Retain the issue list in its reported order."""
        self.issues: list[Issue] = list(issues)
        super().__init__(str(self))

    @classmethod
    def message(cls, message: str) -> ValidationError:
        """Create one issue with no path."""
        return cls([Issue(message)])

    def __str__(self) -> str:
        """Render exactly the reference's multiline validation-message structure."""
        lines = ["invalid config:"]
        for issue in self.issues:
            suffix = f" (at {issue.format_path()})" if issue.path else ""
            lines.append(f"  - {issue.message}{suffix}")
        return "\n".join(lines)


def decode(value: JsonValue, config_type: TypeForm[T]) -> T:
    """Decode declared fields once, retrying a rejected null as an empty object.

    NoConfig accepts any input and returns None. Dataclass defaults apply normally;
    unknown object fields are ignored. Complex schemas may supply their own decoder.
    Annotated integer fields can use Rust widths such as Annotated[int, "u32"].
    """
    if config_type is NoConfig:
        return cast(T, None)
    try:
        return cast(T, _decode(value, config_type, ()))
    except ValidationError:
        if value is not None:
            raise
        return cast(T, _decode({}, config_type, ()))


def decode_json(value: JsonValue) -> JsonValue:
    """Return a raw configuration snapshot without coercing null or sharing containers."""
    return deepcopy(value)


def _decode(value: JsonValue, kind: object, path: tuple[PathSegment, ...]) -> object:
    """Decode one annotated field while carrying its location through nested containers."""
    origin = get_origin(kind)
    arguments = get_args(kind)
    if (
        kind == JsonValue
        or kind is object
        or (isinstance(kind, ForwardRef) and kind.__forward_arg__ == "JsonValue")
    ):
        return deepcopy(value)
    if origin is Annotated:
        base, *metadata = arguments
        for specification in metadata:
            if base is int and isinstance(specification, str) and specification in INTEGER_RANGES:
                if type(value) is not int:
                    raise _type_error(value, specification, path)
                decoded = value
                low, high = INTEGER_RANGES[specification]
                if not low <= decoded <= high:
                    raise ValidationError(
                        [
                            Issue(
                                f"invalid value: integer `{value}`, expected {specification}",
                                list(path),
                            )
                        ]
                    )
                return decoded
        return _decode(value, base, path)
    if origin in (UnionType, Union):
        for alternative in arguments:
            try:
                return _decode(value, alternative, path)
            except ValidationError:
                continue
        raise ValidationError([Issue("data did not match any variant of the union", list(path))])
    if kind is NoneType:
        if value is None:
            return None
        raise _type_error(value, "unit", path)
    if kind is bool and type(value) is bool:
        return value
    if kind is str and isinstance(value, str):
        return value
    if kind is int and type(value) is int:
        return value
    if kind is float and type(value) in (int, float):
        return float(cast(int | float, value))
    if origin is list or kind is list:
        if not isinstance(value, list):
            raise _type_error(value, "a sequence", path)
        element = arguments[0] if arguments else JsonValue
        return [_decode(item, element, (*path, index)) for index, item in enumerate(value)]
    if origin is dict or kind is dict:
        if not isinstance(value, dict):
            raise _type_error(value, "a map", path)
        element = arguments[1] if arguments else JsonValue
        return {key: _decode(item, element, (*path, key)) for key, item in value.items()}
    if isinstance(kind, type) and issubclass(kind, Enum):
        if issubclass(kind, IntEnum) and type(value) is not int:
            raise _type_error(value, "an integer", path)
        try:
            return kind(value)
        except ValueError as error:
            raise ValidationError([Issue(str(error), list(path))]) from error
    if isinstance(kind, type) and is_dataclass(kind):
        if not isinstance(value, dict):
            raise _type_error(value, f"struct {kind.__name__}", path)
        hints = get_type_hints(kind, include_extras=True)
        decoded_fields: dict[str, object] = {}
        for item in fields(kind):
            if not item.init:
                continue
            key = item.metadata.get("alias", item.name)
            if key in value:
                decoded_fields[item.name] = _decode(value[key], hints[item.name], (*path, key))
            elif item.default is MISSING and item.default_factory is MISSING:
                annotation = hints[item.name]
                if get_origin(annotation) in (UnionType, Union) and NoneType in get_args(
                    annotation
                ):
                    decoded_fields[item.name] = None
                else:
                    raise ValidationError([Issue(f"missing field `{key}`", list(path))])
        return kind(**decoded_fields)
    expected = {bool: "a boolean", str: "a string", int: "an integer", float: "f64"}.get(kind)
    if expected is not None:
        raise _type_error(value, expected, path)
    raise TypeError(f"unsupported configuration annotation: {kind!r}; supply a custom decoder")


def _type_error(value: JsonValue, expected: str, path: tuple[PathSegment, ...]) -> ValidationError:
    """Describe a JSON type mismatch while retaining the failing field's path."""
    if value is None:
        actual = "null"
    elif isinstance(value, bool):
        actual = f"boolean `{str(value).lower()}`"
    elif isinstance(value, str):
        actual = f"string {json.dumps(value, ensure_ascii=False)}"
    elif isinstance(value, int):
        actual = f"integer `{value}`"
    elif isinstance(value, float):
        actual = f"floating point `{value}`"
    elif isinstance(value, list):
        actual = "sequence"
    else:
        actual = "map"
    return ValidationError([Issue(f"invalid type: {actual}, expected {expected}", list(path))])


INTEGER_RANGES: dict[str, tuple[int, int]] = {
    "u8": (0, 2**8 - 1),
    "u16": (0, 2**16 - 1),
    "u32": (0, 2**32 - 1),
    "u64": (0, 2**64 - 1),
    "i8": (-(2**7), 2**7 - 1),
    "i16": (-(2**15), 2**15 - 1),
    "i32": (-(2**31), 2**31 - 1),
    "i64": (-(2**63), 2**63 - 1),
}
