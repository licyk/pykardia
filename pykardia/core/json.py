"""Strict JSON boundary with source-compatible number, Unicode and error semantics."""

import json
import math
import re
from dataclasses import dataclass
from decimal import Decimal

from pykardia.core.value import JsonValue


@dataclass(frozen=True)
class Position:
    """A source line and column in the originating parser's coordinate convention."""

    line: int
    column: int


class ParseError(Exception):
    """A document parser error with its original source position."""

    def __init__(self, message: str, position: Position) -> None:
        """Retain the plain diagnostic and position independently."""
        super().__init__(message)
        self.message = message
        self.position = position


class Reader:
    """A character cursor retaining serde_json's byte-column error coordinates."""

    def __init__(self, content: str) -> None:
        """Keep the original source and start before its first character."""
        self.content = content
        self.index = 0
        self.depth = 0

    def error(self, message: str, *, peek: bool = False) -> ParseError:
        """Report either the consumed byte position or the next offending character."""
        end = min(self.index + int(peek), len(self.content))
        prefix = self.content[:end]
        return ParseError(
            message, Position(prefix.count("\n") + 1, len(prefix.rsplit("\n", 1)[-1].encode()))
        )

    def peek(self) -> str:
        """Read the next character without advancing, or an empty string at EOF."""
        return self.content[self.index : self.index + 1]

    def whitespace(self) -> None:
        """Skip only JSON's four permitted whitespace characters."""
        while self.peek() and self.peek() in " \n\r\t":
            self.index += 1

    def value(self) -> JsonValue:
        """Read one JSON value with strict literals and collection grammar."""
        self.whitespace()
        char = self.peek()
        if not char:
            raise self.error("EOF while parsing a value")
        if char == '"':
            return self.string()
        if char in "[{":
            self.depth += 1
            if self.depth >= 128:
                raise self.error("recursion limit exceeded", peek=True)
            result = self.array() if char == "[" else self.object()
            self.depth -= 1
            return result
        if char in "ntf":
            literal, value = {"n": ("null", None), "t": ("true", True), "f": ("false", False)}[char]
            for expected in literal:
                if not self.peek():
                    raise self.error("EOF while parsing a value")
                actual = self.peek()
                self.index += 1
                if actual != expected:
                    raise self.error("expected ident")
            return value
        if char == "-" or "0" <= char <= "9":
            return self.number()
        raise self.error("expected value", peek=True)

    def array(self) -> list[JsonValue]:
        """Read an array, distinguishing empty, truncated and trailing-comma cases."""
        self.index += 1
        self.whitespace()
        values: list[JsonValue] = []
        if not self.peek():
            raise self.error("EOF while parsing a list")
        if self.peek() == "]":
            self.index += 1
            return values
        while True:
            values.append(self.value())
            self.whitespace()
            if self.peek() == "]":
                self.index += 1
                return values
            if not self.peek():
                raise self.error("EOF while parsing a list")
            if self.peek() != ",":
                raise self.error("expected `,` or `]`", peek=True)
            self.index += 1
            self.whitespace()
            if self.peek() == "]":
                raise self.error("trailing comma", peek=True)

    def object(self) -> dict[str, JsonValue]:
        """Read ordered object fields, replacing duplicate values at their original key slot."""
        self.index += 1
        self.whitespace()
        values: dict[str, JsonValue] = {}
        if not self.peek():
            raise self.error("EOF while parsing an object")
        if self.peek() == "}":
            self.index += 1
            return values
        while True:
            if not self.peek():
                raise self.error("EOF while parsing an object")
            if self.peek() != '"':
                raise self.error("key must be a string", peek=True)
            key = self.string()
            self.whitespace()
            if not self.peek():
                raise self.error("EOF while parsing an object")
            if self.peek() != ":":
                raise self.error("expected `:`", peek=True)
            self.index += 1
            values[key] = self.value()
            self.whitespace()
            if self.peek() == "}":
                self.index += 1
                return values
            if not self.peek():
                raise self.error("EOF while parsing an object")
            if self.peek() != ",":
                raise self.error("expected `,` or `}`", peek=True)
            self.index += 1
            self.whitespace()
            if self.peek() == "}":
                raise self.error("trailing comma", peek=True)
            if not self.peek():
                raise self.error("EOF while parsing a value")

    def string(self) -> str:
        """Decode Unicode scalar strings, rejecting unpaired UTF-16 surrogate escapes."""
        self.index += 1
        result: list[str] = []
        while self.peek():
            char = self.peek()
            self.index += 1
            if char == '"':
                return "".join(result)
            if ord(char) < 32:
                raise self.error("control character (\\u0000-\\u001F) found while parsing a string")
            if char != "\\":
                result.append(char)
                continue
            escaped = self.peek()
            if not escaped:
                raise self.error("EOF while parsing a string")
            self.index += 1
            escapes = {
                '"': '"',
                "\\": "\\",
                "/": "/",
                "b": "\b",
                "f": "\f",
                "n": "\n",
                "r": "\r",
                "t": "\t",
            }
            if escaped in escapes:
                result.append(escapes[escaped])
            elif escaped == "u":
                code = self.hex_escape()
                if 0xDC00 <= code <= 0xDFFF:
                    raise self.error("lone leading surrogate in hex escape")
                if 0xD800 <= code <= 0xDBFF:
                    for expected in "\\u":
                        if not self.peek():
                            raise self.error("EOF while parsing a string")
                        actual = self.peek()
                        self.index += 1
                        if actual != expected:
                            raise self.error("unexpected end of hex escape")
                    low = self.hex_escape()
                    if not 0xDC00 <= low <= 0xDFFF:
                        raise self.error("lone leading surrogate in hex escape")
                    code = 0x10000 + ((code - 0xD800) << 10) + low - 0xDC00
                result.append(chr(code))
            else:
                raise self.error("invalid escape")
        raise self.error("EOF while parsing a string")

    def hex_escape(self) -> int:
        """Consume exactly four hex digits before checking an escape's code unit."""
        digits = self.content[self.index : self.index + 4]
        self.index += len(digits)
        if len(digits) < 4:
            raise self.error("EOF while parsing a string")
        if any(char not in "0123456789abcdefABCDEF" for char in digits):
            raise self.error("invalid escape")
        return int(digits, 16)

    def number(self) -> int | float:
        """Parse the source's integer ranges, negative zero and finite floating-point numbers."""
        start = self.index
        if self.peek() == "-":
            self.index += 1
        if not self.peek():
            raise self.error("EOF while parsing a value")
        if not "0" <= self.peek() <= "9":
            raise self.error("invalid number", peek=True)
        if self.peek() == "0":
            self.index += 1
            if self.peek() and "0" <= self.peek() <= "9":
                raise self.error("invalid number", peek=True)
        else:
            while self.peek() and "0" <= self.peek() <= "9":
                self.index += 1
        fractional = False
        for marker in (".", "eE"):
            if not self.peek() or self.peek() not in marker:
                continue
            fractional = True
            self.index += 1
            if marker == "eE" and self.peek() and self.peek() in "+-":
                self.index += 1
            if not self.peek():
                raise self.error("EOF while parsing a value")
            if not "0" <= self.peek() <= "9":
                raise self.error("invalid number", peek=True)
            while self.peek() and "0" <= self.peek() <= "9":
                self.index += 1
        source = self.content[start : self.index]
        if not fractional and source != "-0" and len(source) <= 20:
            integer = int(source)
            if -(2**63) <= integer < 2**64:
                return integer
        floating = float(source)
        if not math.isfinite(floating):
            raise self.error("number out of range")
        return floating


def parse(content: str) -> JsonValue:
    """Decode exactly one complete JSON value and reject any trailing non-whitespace data."""
    reader = Reader(content)
    result = reader.value()
    reader.whitespace()
    if reader.peek():
        raise reader.error("trailing characters", peek=True)
    return result


def json_scalar(value: JsonValue) -> str:
    """Render scalar JSON spelling with normalized exponent digits."""
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, float):
        if 1e-5 <= abs(value) < 1e16 and "e" in text:
            return format(Decimal(text), "f")
        return re.sub(r"e([+-])0+(\d)", r"e\1\2", text)
    return text


def loads(data: bytes) -> JsonValue:
    """Decode external ABI JSON with the pinned parser's byte-position diagnostics."""
    try:
        return parse(data.decode("utf-8"))
    except ParseError as error:
        raise ValueError(
            f"{error.message} at line {error.position.line} column {error.position.column}"
        ) from error


def dumps(value: JsonValue) -> bytes:
    """Serialize compact UTF-8 JSON with the existing source-compatible scalar formatting."""
    return compact(value).encode("utf-8")


def compact(value: JsonValue) -> str:
    """Preserve object insertion order while emitting compact JSON without ASCII escaping."""
    if isinstance(value, dict):
        return (
            "{"
            + ",".join(json_scalar(key) + ":" + compact(item) for key, item in value.items())
            + "}"
        )
    if isinstance(value, list):
        return "[" + ",".join(compact(item) for item in value) + "]"
    return json_scalar(value)
