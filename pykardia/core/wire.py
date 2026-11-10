# SPDX-License-Identifier: MIT OR Apache-2.0
"""Typed guest JSON decoding with serde's streaming field order and error positions."""

from __future__ import annotations

from dataclasses import MISSING, Field, fields, is_dataclass
from types import NoneType, UnionType
from typing import TYPE_CHECKING, TypeVar, Union, cast, get_args, get_origin, get_type_hints

from typing_extensions import TypeForm

from pykardia.core.config import ValidationError, _decode
from pykardia.core.json import ParseError, Reader
from pykardia.core.value import JsonValue

if TYPE_CHECKING:
    from _typeshed import DataclassInstance

T = TypeVar("T")


def from_slice(data: bytes, kind: TypeForm[T]) -> T:
    """Decode a wire struct without config's null-to-object fallback or issue wrapper."""
    reader = TypedReader(data.decode("utf-8"))
    try:
        result = reader.typed(kind)
        reader.whitespace()
        if reader.peek():
            raise reader.error("trailing characters", peek=True)
        return cast(T, result)
    except ParseError as error:
        raise ValueError(
            f"{error.message} at line {error.position.line} column {error.position.column}"
        ) from error


def config(value: JsonValue, kind: TypeForm[T]) -> T:
    """Decode guest config with null fallback and the SDK's unwrapped serde diagnostic."""
    try:
        try:
            return cast(T, _decode(value, kind, ()))
        except ValidationError:
            if value is not None:
                raise
            return cast(T, _decode({}, kind, ()))
    except ValidationError as error:
        raise ValueError(error.issues[0].message) from error


class TypedReader(Reader):
    """Decode wire fields as they arrive instead of losing duplicates in a JSON map."""

    def typed(self, kind: object) -> object:
        """Read the declared wire type, preserving container-entry error positions."""
        self.whitespace()
        origin, args = get_origin(kind), get_args(kind)
        if kind == JsonValue:
            return self.value()
        if origin in (UnionType, Union) and len(args) == 2 and NoneType in args:
            if self.peek() == "n":
                value = self.value()
                if value is None:
                    return None
            kind = next(item for item in args if item is not NoneType)
            return self.typed(kind)
        if isinstance(kind, type) and is_dataclass(kind) and self.peek() in ("{", "["):
            return self.structure(kind)
        if origin is list and self.peek() == "[":
            self.index += 1
            values: list[object] = []
            self.whitespace()
            if self.peek() != "]":
                while True:
                    values.append(self.typed(args[0]))
                    if not self.separator("]"):
                        break
            self.index += 1
            return values
        if self.peek() in ("[", "{") and (
            kind in (str, int, float, bool, NoneType) or origin is list
        ):
            value: JsonValue = [] if self.peek() == "[" else {}
            try:
                return _decode(value, kind, ())
            except ValidationError as error:
                raise self.error(error.issues[0].message) from error
        start = self.index
        value = self.value()
        try:
            return _decode(value, kind, ())
        except ValidationError as error:
            if isinstance(value, (list, dict)):
                self.index = start
            raise self.error(error.issues[0].message) from error

    def structure(self, kind: type[DataclassInstance]) -> object:
        """Read a dataclass from either serde's map or positional struct representation."""
        members = [item for item in fields(kind) if item.init]
        hints = get_type_hints(kind, include_extras=True)
        if self.peek() == "[":
            self.index += 1
            self.whitespace()
            values: dict[str, object] = {}
            for index, item in enumerate(members):
                if self.peek() == "]":
                    if item.default_factory is not MISSING or item.metadata.get("serde_default"):
                        values[item.name] = (
                            item.default_factory()
                            if item.default_factory is not MISSING
                            else item.default
                        )
                        continue
                    raise self.error(
                        f"invalid length {index}, expected struct {kind.__name__} "
                        f"with {len(members)} elements",
                        peek=True,
                    )
                values[item.name] = self.typed(hints[item.name])
                if index + 1 < len(members):
                    self.separator("]")
            self.whitespace()
            if self.peek() != "]":
                raise self.error("trailing characters", peek=True)
            self.index += 1
            return kind(**values)
        by_key = {item.metadata.get("alias", item.name): item for item in members}
        self.index += 1
        self.whitespace()
        values = {}
        if self.peek() != "}":
            while True:
                if not self.peek():
                    raise self.error("EOF while parsing an object")
                if self.peek() != '"':
                    raise self.error("key must be a string", peek=True)
                key = self.string()
                item = by_key.get(key)
                if item is not None and item.name in values:
                    raise self.error(f"duplicate field `{key}`")
                self.whitespace()
                if self.peek() != ":":
                    raise self.error("expected `:`", peek=bool(self.peek()))
                self.index += 1
                if item is None:
                    self.value()
                else:
                    values[item.name] = self.typed(hints[item.name])
                if not self.separator("}"):
                    break
        self.index += 1
        for item in members:
            if item.name not in values:
                self.missing(item, hints[item.name], values)
        return kind(**values)

    def missing(self, item: Field[object], kind: object, values: dict[str, object]) -> None:
        """Apply declared defaults or serde's optional-field behavior at the closing brace."""
        if item.default is not MISSING or item.default_factory is not MISSING:
            return
        args = get_args(kind)
        if get_origin(kind) in (Union, UnionType) and len(args) == 2 and NoneType in args:
            values[item.name] = None
        else:
            raise self.error(f"missing field `{item.metadata.get('alias', item.name)}`")

    def separator(self, closing: str) -> bool:
        """Consume a comma or leave the closing delimiter for the enclosing container."""
        self.whitespace()
        if self.peek() == closing:
            return False
        container = "list" if closing == "]" else "object"
        if not self.peek():
            raise self.error(f"EOF while parsing a {container}")
        if self.peek() != ",":
            raise self.error(f"expected `,` or `{closing}`", peek=True)
        self.index += 1
        self.whitespace()
        if self.peek() == closing:
            raise self.error("trailing comma", peek=True)
        return True
