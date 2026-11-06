"""File format boundaries, validation issues and deterministic JSON output."""

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from pykardia.core.config import PathSegment
from pykardia.core.json import parse as parse_json
from pykardia.core.value import JsonValue
from pykardia.include import yaml
from pykardia.include.tree import group_list
from pykardia.include.yaml import Position, Syntax


class Stage(Enum):
    """The boundary at which an included file failed."""

    Read = "read"
    Parse = "parse"
    Validate = "validate"


@dataclass
class ConfigIssue:
    """A file-level diagnostic and its source position."""

    message: str
    position: Position


class ConfigFileError(Exception):
    """An included file that could not be read, parsed or structurally validated."""

    def __init__(
        self,
        stage: Stage,
        path: Path,
        cause: str | None = None,
        issues: list[ConfigIssue] | None = None,
        not_found: bool = False,
    ) -> None:
        """Retain the failing stage, file path and structured source issues."""
        self.stage, self.path, self.cause = stage, path, cause
        self.issues = issues if issues is not None else []
        self.not_found = not_found
        super().__init__(str(self))

    def __str__(self) -> str:
        """Render the source's file error header and issue positions."""
        text = f"failed to {self.stage.value} config file {self.path}"
        if self.cause is not None:
            text += ": " + self.cause
        for issue in self.issues:
            text += (
                f"\n  - {issue.message} (at {self.path}:"
                f"{issue.position.line}:{issue.position.column})"
            )
        return text


def parse_document(content: str, filename: Path) -> tuple[list[JsonValue], Syntax]:
    """Parse and validate the file's array and entry-object shape once at the boundary."""
    try:
        if filename.suffix == ".json":
            data = parse_json(content)
            syntax = json_syntax(content)
        else:
            data, syntax = yaml.parse(content)
    except yaml.ParseError as error:
        raise ConfigFileError(
            Stage.Parse, filename, issues=[ConfigIssue(error.message, error.position)]
        ) from error
    if not isinstance(data, list):
        raise ConfigFileError(
            Stage.Validate,
            filename,
            issues=[
                ConfigIssue(
                    "config file must be a top-level array", syntax.locate([]) or Position(1, 1)
                )
            ],
        )
    issues: list[ConfigIssue] = []
    for path in invalid_entries(data):
        issues.append(ConfigIssue("entry must be an object", syntax.locate(path) or Position(1, 1)))
    if issues:
        raise ConfigFileError(Stage.Validate, filename, issues=issues)
    return data, syntax


def json_syntax(content: str) -> Syntax:
    """Parse positions for JSON or freshly dumped YAML, falling back to the empty document."""
    try:
        return yaml.parse(content)[1]
    except yaml.ParseError:
        return yaml.parse("")[1]


def invalid_entries(
    entries: list[JsonValue], prefix: list[PathSegment] | None = None
) -> list[list[PathSegment]]:
    """Find nonobject entries recursively within structural group arrays."""
    result: list[list[PathSegment]] = []
    for index, options in enumerate(entries):
        path = [*(prefix or []), index]
        if not isinstance(options, dict):
            result.append(path)
        else:
            members = group_list(options)
            if members is not None:
                result.extend(invalid_entries(members, [*path, "config"]))
    return result


def pretty_json(value: JsonValue, indent: int = 0) -> str:
    """Render serde_json-style two-space JSON without a trailing newline."""
    padding = " " * indent
    child = " " * (indent + 2)
    if isinstance(value, list):
        return (
            "[\n"
            + ",\n".join(child + pretty_json(item, indent + 2) for item in value)
            + "\n"
            + padding
            + "]"
            if value
            else "[]"
        )
    if isinstance(value, dict):
        return (
            "{\n"
            + ",\n".join(
                child + json.dumps(key, ensure_ascii=False) + ": " + pretty_json(item, indent + 2)
                for key, item in value.items()
            )
            + "\n"
            + padding
            + "}"
            if value
            else "{}"
        )
    return yaml.json_scalar(value)
