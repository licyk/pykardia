"""Validated entry views and source-preserving option mutations."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from typing import TypeAlias

from pykardia.core.config import Issue
from pykardia.core.plugin import Inject
from pykardia.core.value import JsonValue, json_equal

SEP = ":"
EXPR_KEY = "__jsExpr"
Disabled: TypeAlias = bool | str
IsolateLabel: TypeAlias = str | None


def is_expr(value: JsonValue) -> bool:
    """Recognize expression objects by marker presence, including malformed markers."""
    return isinstance(value, dict) and EXPR_KEY in value


def expr_source(value: JsonValue) -> str | None:
    """Read a string expression source, returning None for an invalid marker value."""
    source = value.get(EXPR_KEY) if isinstance(value, dict) else None
    return source if isinstance(source, str) else None


@dataclass
class EntryOptions:
    """A typed view of accepted options, separate from the preserved source object."""

    id: str
    name: str
    config: JsonValue
    group: bool
    disabled: Disabled | None
    inject: Inject
    intercept: dict[str, JsonValue]
    isolate: list[tuple[str, IsolateLabel]]

    @classmethod
    def read(cls, value: JsonValue) -> EntryOptions:
        """Read already validated options with the source's missing and null defaults."""
        mapping = value if isinstance(value, dict) else {}
        identifier = mapping.get("id")
        name = mapping.get("name")
        disabled = mapping.get("disabled")
        source = expr_source(disabled)
        intercept = mapping.get("intercept")
        isolation = mapping.get("isolate")
        return cls(
            id=identifier if isinstance(identifier, str) else "",
            name=name if isinstance(name, str) else "",
            config=deepcopy(mapping.get("config")),
            group=mapping.get("group") is True,
            disabled=source
            if source is not None
            else (disabled is True if disabled is not None else None),
            inject=read_inject(mapping.get("inject")),
            intercept=deepcopy(intercept) if isinstance(intercept, dict) else {},
            isolate=[
                (key, label if isinstance(label, str) else None) for key, label in isolation.items()
            ]
            if isinstance(isolation, dict)
            else [],
        )


def read_inject(value: JsonValue) -> Inject:
    """Build ordered declarations from an array or an object with optional metadata."""
    specification = Inject()
    if isinstance(value, list):
        for name in value:
            if isinstance(name, str):
                specification = specification.with_(name)
    elif isinstance(value, dict):
        for name, metadata in value.items():
            specification = (
                specification.with_(name)
                if metadata is None
                else specification.with_meta(name, metadata)
            )
    return specification


def merge(target: JsonValue, patch: dict[str, JsonValue]) -> None:
    """Apply a shallow patch in place, deleting null keys and cloning retained values."""
    if isinstance(target, dict):
        for key, value in patch.items():
            if value is None:
                target.pop(key, None)
            else:
                target[key] = deepcopy(value)


def sort_keys(value: JsonValue) -> None:
    """Reorder an object in place as id, name, alphabetical other keys, then config."""
    if isinstance(value, dict):
        entries = sorted(value.items(), key=lambda item: (key_rank(item[0]), item[0]))
        value.clear()
        value.update(entries)


def key_rank(key: str) -> int:
    """Rank the reserved output fields around the alphabetical ordinary fields."""
    return {"id": 0, "name": 1, "config": 3}.get(key, 2)


def diff_keys(a: JsonValue, b: JsonValue) -> list[str]:
    """List differing keys in old-object order followed by new-only keys."""
    left = a if isinstance(a, dict) else {}
    right = b if isinstance(b, dict) else {}
    keys = list(left) + [key for key in right if key not in left]
    return [
        key
        for key in keys
        if key not in left or key not in right or not json_equal(left[key], right[key])
    ]


def validate_options(options: JsonValue, deep: bool) -> list[Issue]:
    """Validate external entry fields once, optionally recursing into group children."""
    if not isinstance(options, dict) or is_expr(options):
        return [Issue("entry must be an object")]
    issues: list[Issue] = []

    def check(
        key: str, expected: str, valid: Callable[[JsonValue], bool], required: bool = False
    ) -> None:
        """Append a field error without rejecting unknown source keys."""
        value = options.get(key)
        if value is None:
            if required:
                issues.append(Issue(f"{key} is required", [key]))
        elif not valid(value):
            issues.append(Issue(f"{key} must be {expected}", [key]))

    check("id", "a string", lambda value: isinstance(value, str))
    check("name", "a string", lambda value: isinstance(value, str), True)
    check("group", "a boolean", lambda value: isinstance(value, bool))
    check(
        "disabled",
        "a boolean",
        lambda value: isinstance(value, bool) or expr_source(value) is not None,
    )
    is_group = options.get("group") is True
    if is_group:
        check("config", "an array", lambda value: isinstance(value, list), True)
    check("inject", "an array or an object", lambda value: isinstance(value, list) or plain(value))
    check("intercept", "an object", plain)
    check("isolate", "an object", plain)
    identifier = options.get("id")
    if isinstance(identifier, str) and SEP in identifier:
        issues.append(Issue(f'id must not contain "{SEP}"', ["id"]))
    injection = options.get("inject")
    if isinstance(injection, list):
        for index, name in enumerate(injection):
            if not isinstance(name, str):
                issues.append(Issue(f"inject.{index} must be a string", ["inject", index]))
    isolation = options.get("isolate")
    if isinstance(isolation, dict) and not is_expr(isolation):
        for name, label in isolation.items():
            if label is not True and not isinstance(label, str):
                issues.append(Issue(f"isolate.{name} must be true or a string", ["isolate", name]))
    children = options.get("config")
    if deep and is_group and isinstance(children, list):
        for index, child in enumerate(children):
            for issue in validate_options(child, True):
                issues.append(Issue(issue.message, ["config", index, *issue.path]))
    return issues


def plain(value: JsonValue) -> bool:
    """Distinguish an ordinary options object from a marked expression object."""
    return isinstance(value, dict) and not is_expr(value)


class EntryValidationError(Exception):
    """Rejected entry options with source-compatible issue rendering."""

    def __init__(self, issues: list[Issue]) -> None:
        """Retain the ordered issues for later file-location enrichment."""
        self.issues = issues
        super().__init__(str(self))

    def __str__(self) -> str:
        """Render field paths below the entry validation heading."""
        lines = ["invalid entry options:"]
        for issue in self.issues:
            suffix = f" (at {issue.format_path()})" if issue.path else ""
            lines.append(f"  - {issue.message}{suffix}")
        return "\n".join(lines)
