"""Tree persistence boundaries and source-location values."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from pykardia.core.config import PathSegment
from pykardia.core.value import JsonValue
from pykardia.loader.catalog import Resolver

if TYPE_CHECKING:
    from pykardia.loader.handles import GroupHandle


class Missing(Enum):
    """Distinguish omitted options or source values from an explicitly written JSON null."""

    VALUE = "missing"


MISSING = Missing.VALUE


@dataclass
class Location:
    """A source file position with one-based Unicode character line and column."""

    file: Path
    line: int
    column: int

    def __str__(self) -> str:
        """Render the source's file:line:column diagnostic form."""
        return f"{self.file}:{self.line}:{self.column}"


@dataclass
class EntryChange:
    """A committed tree mutation, with absent old/new values indicating create/remove."""

    id: str
    group: GroupHandle
    from_: GroupHandle | None
    options: JsonValue
    legacy: JsonValue


class TreeBackend:
    """Persistence and location hooks shared by entries of one tree."""

    def commit(self, change: EntryChange) -> None:
        """Observe a tree mutation synchronously after its source data has changed."""

    def resolver(self) -> Resolver | None:
        """Return a tree-specific resolver, or inherit the loader's resolver."""
        return None

    def base_dir(self) -> Path | None:
        """Return this tree's relative-path base, or inherit its containing tree's base."""
        return None

    def handle(self) -> object | None:
        """Return the owning backend object for retrieval through its tree handle."""
        return None

    def locate(
        self, id: str, path: list[PathSegment], value: JsonValue | Missing = MISSING
    ) -> Location | None:
        """Locate a matching source value, or the nearest written ancestor when omitted."""
        return None

    def locate_expr(
        self, id: str, path: list[PathSegment], expr: str, offset: int
    ) -> Location | None:
        """Locate a character of an expression only if that expression is still written there."""
        return None


class MemoryTree(TreeBackend):
    """An in-memory backend with no persistence or source-location hooks."""


def value_at(value: JsonValue, path: list[PathSegment]) -> JsonValue | Missing:
    """Read a nested JSON value while distinguishing missing paths from explicit null."""
    for part in path:
        if isinstance(part, str) and isinstance(value, dict) and part in value:
            value = value[part]
        elif type(part) is int and isinstance(value, list) and 0 <= part < len(value):
            value = value[part]
        else:
            return MISSING
    return value
