"""YAML Core-schema values, source positions and source-compatible block output."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterator
from copy import deepcopy
from dataclasses import dataclass
from typing import cast

import yaml as parser
from yaml.error import Mark
from yaml.events import (
    AliasEvent,
    DocumentEndEvent,
    Event,
    MappingEndEvent,
    MappingStartEvent,
    ScalarEvent,
    SequenceEndEvent,
    SequenceStartEvent,
    StreamEndEvent,
)
from yaml.tokens import DirectiveToken, ScalarToken

from pykardia.core.config import PathSegment
from pykardia.core.json import ParseError, Position, json_scalar
from pykardia.core.value import JsonValue
from pykardia.loader import EXPR_KEY, expr_source
from pykardia.loader._unicode import WHITESPACE
from pykardia.loader.expr import character_in

CORE = "tag:yaml.org,2002:"
FLOAT = re.compile(r"[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?\Z")


@dataclass
class Node:
    """Source structure retained independently of resolved JSON values."""

    start: Position
    index: int
    style: str | None = None
    items: list[Node] | None = None
    entries: list[tuple[str, Position, Node]] | None = None

    def step(self, segment: PathSegment) -> tuple[Position, Node] | None:
        """Find the first written key or indexed member without ancestor fallback."""
        if isinstance(segment, str) and self.entries is not None:
            return next(((pos, node) for key, pos, node in self.entries if key == segment), None)
        if type(segment) is int and self.items is not None and 0 <= segment < len(self.items):
            child = self.items[segment]
            return child.start, child
        return None


@dataclass
class Syntax:
    """A parsed document's location tree and original character source."""

    root: Node | None
    content: str

    def locate(self, path: list[PathSegment]) -> Position | None:
        """Locate scalars at values and collections at keys, falling back to written ancestors."""
        node = self.root
        if node is None:
            return None
        anchor = node.start
        for segment in path:
            child = node.step(segment)
            if child is None:
                break
            anchor, node = child
        return node.start if node.items is None and node.entries is None else anchor

    def locate_expr(self, path: list[PathSegment], offset: int) -> Position | None:
        """Map decoded expression character offsets through quotes, escapes and line folding."""
        node = self.root
        if node is None:
            return None
        for segment in path:
            child = node.step(segment)
            if child is None:
                return None
            node = child[1]
        expression = node.step(EXPR_KEY)
        if expression is not None:
            node = expression[1]
        if node.items is not None or node.entries is not None:
            return None
        if node.style not in (None, "'", '"'):
            return node.start
        index = node.index + (node.style is not None)
        for _ in range(offset):
            if index >= len(self.content):
                break
            char = self.content[index]
            if char == "\n":
                index += 1
                while index < len(self.content) and self.content[index] in " \t":
                    index += 1
            elif node.style == "'" and self.content[index : index + 2] == "''":
                index += 2
            elif node.style == '"' and char == "\\":
                following = self.content[index + 1 : index + 2]
                index += {"x": 4, "u": 6, "U": 10}.get(following, 2)
            else:
                index += 1
        return advance(node.start, self.content[node.index : index])


def advance(start: Position, content: str) -> Position:
    """Advance one-based character coordinates through the original source substring."""
    line, column = start.line, start.column
    for char in content:
        if char == "\n":
            line, column = line + 1, 1
        else:
            column += 1
    return Position(line, column)


def parse(content: str) -> tuple[JsonValue, Syntax]:
    """Parse only the first document using Core schema regardless of YAML version directives."""
    reader = Reader(content)
    try:
        for event in reader.events:
            if isinstance(event, (ScalarEvent, AliasEvent, SequenceStartEvent, MappingStartEvent)):
                value, node = reader.node(event)
                # Consume document end so a trailing malformed token still reports a parser error.
                for ending in reader.events:
                    if isinstance(ending, (DocumentEndEvent, StreamEndEvent)):
                        break
                return value, Syntax(node, content)
            if isinstance(event, (DocumentEndEvent, StreamEndEvent)):
                break
    except parser.MarkedYAMLError as error:
        raise parser_error(error) from error
    return None, Syntax(Node(Position(1, 1), 0), content)


class Reader:
    """Resolve streaming parser events while retaining anchors and original source nodes."""

    def __init__(self, content: str) -> None:
        """Keep a fresh event stream and document-local completed-anchor table."""
        self.content = content
        self.events: Iterator[Event] = iter(parser.parse(content, Loader=CoreLoader))
        self.declared: set[str] = set()
        self.anchors: dict[str, tuple[JsonValue, Node]] = {}

    def node(self, event: Event) -> tuple[JsonValue, Node]:
        """Consume one scalar or collection, resolving aliases only after anchors complete."""
        mark = cast(Mark, event.start_mark)
        position = Position(mark.line + 1, mark.column + 1)
        index = mark.index
        if isinstance(event, AliasEvent):
            anchored = self.anchors.get(event.anchor)
            if anchored is None:
                message = (
                    "unknown alias"
                    if event.anchor in self.declared
                    else "while parsing node, found unknown anchor"
                )
                raise ParseError(message, position)
            return deepcopy(anchored)
        if (
            isinstance(event, (ScalarEvent, SequenceStartEvent, MappingStartEvent))
            and event.anchor is not None
        ):
            self.declared.add(event.anchor)
            self.anchors.pop(event.anchor, None)
        if isinstance(event, ScalarEvent):
            value = scalar(event.value, event.style, event.tag, position)
            start = index
            while index < len(self.content) and self.content[index] in "&!":
                while index < len(self.content) and not is_whitespace(self.content[index]):
                    index += 1
                while index < len(self.content) and is_whitespace(self.content[index]):
                    index += 1
            node = Node(advance(position, self.content[start:index]), index, event.style)
        elif isinstance(event, SequenceStartEvent):
            check_collection_tag(event.tag, "seq", position)
            items: list[Node] = []
            values: list[JsonValue] = []
            for child in self.events:
                if isinstance(child, SequenceEndEvent):
                    break
                item, item_node = self.node(child)
                values.append(item)
                items.append(item_node)
            value, node = values, Node(position, index, items=items)
        elif isinstance(event, MappingStartEvent):
            check_collection_tag(event.tag, "map", position)
            mapping: dict[str, JsonValue] = {}
            entries: list[tuple[str, Position, Node]] = []
            for child in self.events:
                if isinstance(child, MappingEndEvent):
                    break
                key, key_node = self.node(child)
                if isinstance(key, (list, dict)):
                    raise ParseError("complex keys are not supported", key_node.start)
                name = key if isinstance(key, str) else json_scalar(key)
                item, item_node = self.node(next(self.events))
                mapping[name] = item
                entries.append((name, key_node.start, item_node))
            value, node = mapping, Node(position, index, entries=entries)
        else:
            raise ParseError("unexpected document event", position)
        if event.anchor is not None:
            self.anchors[event.anchor] = deepcopy((value, node))
        return value, node


def check_collection_tag(tag: str | None, expected: str, position: Position) -> None:
    """Accept only the standard tag for this collection kind."""
    if tag is not None and tag != CORE + expected:
        raise ParseError(f"unresolved tag: {tag}", position)


def scalar(value: str, style: str | None, tag: str | None, position: Position) -> JsonValue:
    """Resolve explicit tags or plain Core scalars without YAML 1.1 coercions."""
    if tag is not None:
        if not tag.startswith(CORE):
            raise ParseError(f"unresolved tag: {tag}", position)
        suffix = tag[len(CORE) :]
        if suffix == "js":
            return {EXPR_KEY: value}
        if suffix == "str":
            return value
        if suffix in ("null", "bool", "int", "float"):
            resolved = resolve_plain(value)
            if isinstance(resolved, str):
                raise ParseError(f"invalid !!{suffix} value: {value}", position)
            return resolved
        raise ParseError(f"unresolved tag: !!{suffix}", position)
    return resolve_plain(value) if style is None else value


def resolve_plain(value: str) -> JsonValue:
    """Resolve source Core-schema scalars, leaving unsupported/nonfinite numbers as strings."""
    if value in ("", "~", "null", "Null", "NULL"):
        return None
    if value in ("true", "True", "TRUE"):
        return True
    if value in ("false", "False", "FALSE"):
        return False
    unsigned = value[1:] if value.startswith(("-", "+")) else value
    if unsigned and all("0" <= char <= "9" for char in unsigned):
        number = int(value)
        if -(2**63) <= number < 2**63:
            return number
        floating = float(value)
        return floating if math.isfinite(floating) else value
    for prefix, radix, digits in (("0o", 8, "01234567"), ("0x", 16, "0123456789abcdefABCDEF")):
        if value.startswith(prefix):
            rest = value[2:]
            if rest and all(char in digits for char in rest):
                number = int(rest, radix)
                return number if number < 2**63 else value
    if FLOAT.fullmatch(value):
        floating = float(value)
        if math.isfinite(floating):
            return floating
    return value


def dump(value: JsonValue) -> str:
    """Dump block YAML with the source's indentation, expression tags and terminal newline."""
    output: list[str] = []
    write_block(output, value, 0)
    return "".join(output)


def write_block(output: list[str], value: JsonValue, indent: int) -> None:
    """Write sequences, mappings and scalar leaves at their source indentation."""
    if isinstance(value, list) and value:
        for item in value:
            output.append(" " * indent + "-")
            if isinstance(item, dict) and item and expr_source(item) is None:
                output.append(" ")
                write_map(output, item, indent + 2, True)
            elif isinstance(item, list) and item:
                output.append("\n")
                write_block(output, item, indent + 2)
            else:
                output.append(" " + inline(item) + "\n")
    elif isinstance(value, dict) and value and expr_source(value) is None:
        write_map(output, value, indent, False)
    else:
        output.append(" " * indent + inline(value) + "\n")


def write_map(output: list[str], value: dict[str, JsonValue], indent: int, continued: bool) -> None:
    """Write map fields in insertion order, continuing a sequence item's first field inline."""
    for index, (key, item) in enumerate(value.items()):
        if index or not continued:
            output.append(" " * indent)
        output.append(string(key) + ":")
        if isinstance(item, dict) and item and expr_source(item) is None:
            output.append("\n")
            write_map(output, item, indent + 2, False)
        elif isinstance(item, list) and item:
            output.append("\n")
            write_block(output, item, indent)
        else:
            output.append(" " + inline(item) + "\n")


def inline(value: JsonValue) -> str:
    """Write scalar values and empty containers, using !!js for valid expression objects."""
    expression = expr_source(value)
    if expression is not None:
        return "!!js " + quoted(expression)
    if isinstance(value, str):
        return string(value)
    if isinstance(value, list):
        return "[]"
    if isinstance(value, dict):
        return "{}"
    return json_scalar(value)


def quoted(text: str) -> str:
    """Use JSON double quotes for control characters and doubled single quotes otherwise."""
    if any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in text):
        return json.dumps(text, ensure_ascii=False)
    return "'" + text.replace("'", "''") + "'"


def string(text: str) -> str:
    """Use plain text only when the source dumper can read it back as the same string."""
    safe = (
        bool(text)
        and text[0] not in "-?:,[]{}#&*!|>'\"%@`"
        and not is_whitespace(text[0])
        and not is_whitespace(text[-1])
        and ": " not in text
        and " #" not in text
        and not text.endswith(":")
        and not any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in text)
        and isinstance(resolve_plain(text), str)
        and text not in (".inf", ".nan", "-.inf", ".Inf", ".NaN")
    )
    return text if safe else quoted(text)


def is_whitespace(char: str) -> bool:
    """Use the pinned Rust Unicode whitespace classification for source text."""
    return character_in(char, WHITESPACE)


class CoreLoader(parser.BaseLoader):
    """Adapt token properties while retaining PyYAML's pure Python scanner and parser."""

    def scan_directive(self) -> DirectiveToken:
        """Accept version directives without selecting a different scalar schema."""
        token = super().scan_directive()
        if token.name == "YAML":
            token.value = (1, token.value[1])
        return token

    def scan_block_scalar(self, style: str) -> ScalarToken:
        """Locate untagged block scalars at content, matching the reference token span."""
        parent_indent = self.indent
        token = super().scan_block_scalar(style)
        mark = token.start_mark
        source = mark.buffer
        start = mark.pointer
        end_header = source.find("\n", start)
        if end_header < 0:
            return token
        header = source[start:end_header]
        explicit = re.search(r"[1-9]", header.split("#", 1)[0])
        indent = max(parent_indent, 0) + int(explicit[0]) if explicit else None
        cursor = end_header + 1
        while cursor < len(source):
            end = source.find("\n", cursor)
            if end < 0:
                end = len(source)
            line = source[cursor:end].rstrip("\0\r")
            if line.strip(" "):
                width = len(line) - len(line.lstrip(" ")) if indent is None else indent
                cursor += min(width, len(line) - len(line.lstrip(" ")))
                position = advance(Position(mark.line + 1, mark.column + 1), source[start:cursor])
                token.start_mark = Mark(
                    mark.name,
                    mark.index + cursor - start,
                    position.line - 1,
                    position.column - 1,
                    source,
                    cursor,
                )
                return token
            cursor = end + 1
        return token


def parser_error(error: parser.MarkedYAMLError) -> ParseError:
    """Map equivalent scanner failures to the pinned parser's diagnostic and marker."""
    problem = error.problem or str(error)
    context = error.context or ""
    mark = error.problem_mark or error.context_mark
    position = Position(mark.line + 1, mark.column + 1) if mark is not None else Position(1, 1)
    if context in ("while scanning a quoted scalar", "while scanning a double-quoted scalar"):
        if problem == "found unexpected end of stream":
            problem = "while scanning a quoted scalar, found unexpected end of stream"
        elif problem.startswith("found unknown escape character"):
            problem = "while parsing a quoted scalar, found unknown escape character"
        if error.context_mark is not None:
            position = Position(error.context_mark.line + 1, error.context_mark.column + 1)
    elif "but got ':'" in problem:
        problem = "illegal placement of ':' indicator"
    elif problem.startswith("expected ',' or ']'"):
        problem = "while parsing a flow sequence, expected ',' or ']'"
    elif problem.startswith("expected ',' or '}'"):
        problem = "while parsing a flow mapping, did not find expected ',' or '}'"
    elif problem.startswith("expected <block end>"):
        problem = f"{context}, did not find expected key"
    elif problem.startswith("expected the node content"):
        problem = "while parsing a node, did not find expected node content"
    elif problem == "mapping values are not allowed here":
        problem = "mapping values are not allowed in this context"
    elif problem.startswith("found character '\\t'"):
        problem = "tabs disallowed within this context (block indentation)"
        position = Position(position.line, position.column + 1)
    if "<stream end>" in (error.problem or "") and position.column != 1:
        position = Position(position.line + 1, 1)
    return ParseError(problem, position)
