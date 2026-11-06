"""The source JavaScript-like JSON expression language and checked service reads."""

from __future__ import annotations

import math
import re
from bisect import bisect_right
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from typing import TypeAlias

from pykardia.core.config import PathSegment
from pykardia.core.context import Context
from pykardia.core.value import JsonValue, json_equal
from pykardia.loader._unicode import ALPHABETIC, ALPHANUMERIC, WHITESPACE
from pykardia.loader.options import expr_source

PUNCTS = (
    "===",
    "!==",
    "==",
    "!=",
    "<=",
    ">=",
    "&&",
    "||",
    "??",
    "?.",
    "?",
    ":",
    ".",
    ",",
    "(",
    ")",
    "[",
    "]",
    "{",
    "}",
    "<",
    ">",
    "!",
    "+",
    "-",
    "*",
    "/",
    "%",
)
LEVELS = (
    ("??",),
    ("||",),
    ("&&",),
    ("===", "!==", "==", "!="),
    ("<=", ">=", "<", ">"),
    ("+", "-"),
    ("*", "/", "%"),
)
FLOAT_PATTERN = re.compile(
    r"[+-]?(?:(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?|(?i:inf(?:inity)?|nan))\Z"
)


class ExprError(Exception):
    """An expression failure with its source field path and Unicode character offset."""

    def __init__(self, path: list[PathSegment], expr: str, offset: int, message: str) -> None:
        """Retain the failed expression and source-location information."""
        self.path = list(path)
        self.expr = expr
        self.offset = offset
        self.message = message
        super().__init__(f"cannot evaluate `{expr}`: {message}")


class Fault(Exception):
    """An internal evaluation failure carrying the failing service identifier offset."""

    def __init__(self, error: Exception, offset: int = 0) -> None:
        """Keep the original error so plain evaluate can rethrow it without wrapping."""
        self.error = error
        self.offset = offset
        super().__init__(str(error))


def evaluate(ctx: Context, source: str) -> JsonValue:
    """Evaluate a source expression through the caller's checked service JSON views."""
    try:
        return run(ctx, source)
    except Fault as fault:
        raise fault.error from None


def evaluate_at(ctx: Context, source: str, path: list[PathSegment]) -> JsonValue:
    """Evaluate an entry field and enrich errors with its path and character offset."""
    try:
        return run(ctx, source)
    except Fault as fault:
        raise ExprError(path, source, fault.offset, str(fault.error)) from None


def run(ctx: Context, source: str) -> JsonValue:
    """Parse the complete expression before performing any checked service reads."""
    try:
        parser = Parser(lex(source))
        expression = parser.ternary()
        if parser.position < len(parser.tokens):
            raise syntax(source, "unexpected token")
        return eval_expr(ctx, expression)
    except Fault:
        raise
    except Exception as error:
        raise Fault(error) from error


def interpolate(ctx: Context, value: JsonValue) -> JsonValue:
    """Replace every valid expression marker recursively without mutating the source."""
    return interpolate_at(ctx, value, [])


def interpolate_at(ctx: Context, value: JsonValue, path: list[PathSegment]) -> JsonValue:
    """Interpolate nested values while retaining field and array-index error paths."""
    source = expr_source(value)
    if source is not None:
        return evaluate_at(ctx, source, path)
    if isinstance(value, list):
        return [interpolate_at(ctx, item, [*path, index]) for index, item in enumerate(value)]
    if isinstance(value, dict):
        return {key: interpolate_at(ctx, item, [*path, key]) for key, item in value.items()}
    return value


def has_expr(value: JsonValue) -> bool:
    """Report whether a valid string expression occurs anywhere in a JSON value."""
    if expr_source(value) is not None:
        return True
    if isinstance(value, list):
        return any(has_expr(item) for item in value)
    if isinstance(value, dict):
        return any(has_expr(item) for item in value.values())
    return False


def syntax(source: str, message: str) -> ValueError:
    """Build the source-inclusive lexer or trailing-token diagnostic."""
    return ValueError(f"syntax error in `{source}`: {message}")


@dataclass
class Token:
    """One lexical token with a Unicode scalar offset into the original source."""

    kind: str
    value: str | float
    offset: int


def character_in(char: str, table: tuple[int, ...]) -> bool:
    """Classify a character using the pinned Rust Unicode property interval endpoints."""
    return bisect_right(table, ord(char)) % 2 == 1


def lex(source: str) -> list[Token]:
    """Tokenize the source grammar, preserving its intentionally limited escape rules."""
    tokens: list[Token] = []
    i = 0
    while i < len(source):
        char = source[i]
        start = i
        if character_in(char, WHITESPACE):
            i += 1
        elif char in "0123456789" or (
            char == "." and i + 1 < len(source) and source[i + 1] in "0123456789"
        ):
            while i < len(source) and (
                source[i].isascii() and source[i].isalnum() or source[i] == "."
            ):
                i += 1
            text = source[start:i]
            if FLOAT_PATTERN.fullmatch(text) is None:
                raise syntax(source, f"invalid number {text}")
            tokens.append(Token("number", float(text), start))
        elif char in "'\"`":
            text = ""
            i += 1
            while True:
                if i >= len(source):
                    raise syntax(source, "unterminated string")
                next_char = source[i]
                i += 1
                if next_char == char:
                    break
                if next_char == "\\":
                    if i >= len(source):
                        raise syntax(source, "unterminated string")
                    escaped = source[i]
                    i += 1
                    text += {"n": "\n", "t": "\t", "r": "\r", "0": "\0"}.get(escaped, escaped)
                else:
                    text += next_char
            tokens.append(Token("string", text, start))
        elif character_in(char, ALPHABETIC) or char in "_$":
            while i < len(source) and (character_in(source[i], ALPHANUMERIC) or source[i] in "_$"):
                i += 1
            tokens.append(Token("ident", source[start:i], start))
        else:
            punct = next((punct for punct in PUNCTS if source.startswith(punct, i)), None)
            if punct is None:
                raise syntax(source, f"unexpected `{char}`")
            tokens.append(Token("punct", punct, i))
            i += len(punct)
    return tokens


@dataclass
class Literal:
    """An already decoded JSON literal."""

    value: JsonValue


@dataclass
class Ident:
    """A free service identifier and the offset of its first character."""

    name: str
    offset: int


@dataclass
class Array:
    """An ordered array literal."""

    items: list[Expr]


@dataclass
class Object:
    """Object entries evaluated in source order, including repeated keys."""

    entries: list[tuple[str, Expr]]


@dataclass
class Member:
    """An ordinary or optional property access."""

    object: Expr
    key: Expr
    optional: bool


@dataclass
class Unary:
    """A prefix boolean or numeric operator."""

    operator: str
    operand: Expr


@dataclass
class Binary:
    """A binary operator whose operands may be evaluated conditionally."""

    operator: str
    left: Expr
    right: Expr


@dataclass
class Ternary:
    """A conditional expression that evaluates only its selected branch."""

    condition: Expr
    then: Expr
    otherwise: Expr


Expr: TypeAlias = Literal | Ident | Array | Object | Member | Unary | Binary | Ternary


class Parser:
    """Recursive descent parser with the reference's exact precedence levels."""

    def __init__(self, tokens: list[Token]) -> None:
        """Start at the first token of the complete lexical input."""
        self.tokens = tokens
        self.position = 0

    def peek(self) -> Token | None:
        """Return the current token without advancing past the end."""
        return self.tokens[self.position] if self.position < len(self.tokens) else None

    def eat(self, punct: str) -> bool:
        """Consume exactly the requested punctuation if it is next."""
        token = self.peek()
        if token is not None and token.kind == "punct" and token.value == punct:
            self.position += 1
            return True
        return False

    def expect(self, punct: str) -> None:
        """Require punctuation using the reference parser diagnostic."""
        if not self.eat(punct):
            raise ValueError(f"syntax error: expected `{punct}`")

    def ternary(self) -> Expr:
        """Parse a right-associative conditional above the binary precedence levels."""
        condition = self.binary(0)
        if not self.eat("?"):
            return condition
        then = self.ternary()
        self.expect(":")
        return Ternary(condition, then, self.ternary())

    def binary(self, level: int) -> Expr:
        """Climb from low to high precedence while retaining left associativity."""
        if level >= len(LEVELS):
            return self.unary()
        left = self.binary(level + 1)
        while (
            (token := self.peek()) is not None
            and token.kind == "punct"
            and token.value in LEVELS[level]
        ):
            self.position += 1
            left = Binary(str(token.value), left, self.binary(level + 1))
        return left

    def unary(self) -> Expr:
        """Read recursive prefix operators before postfix member access."""
        for operator in ("!", "-", "+"):
            if self.eat(operator):
                return Unary(operator, self.unary())
        return self.postfix()

    def postfix(self) -> Expr:
        """Extend a primary with ordinary, indexed or optional property accesses."""
        expression = self.primary()
        while True:
            optional = self.eat("?.")
            if optional or self.eat("."):
                if optional and self.eat("["):
                    index = self.ternary()
                    self.expect("]")
                    expression = Member(expression, index, True)
                    continue
                token = self.peek()
                if token is None or token.kind != "ident":
                    raise ValueError("syntax error: expected a property name")
                self.position += 1
                expression = Member(expression, Literal(str(token.value)), optional)
            elif self.eat("["):
                index = self.ternary()
                self.expect("]")
                expression = Member(expression, index, False)
            else:
                return expression

    def primary(self) -> Expr:
        """Read literals, free identifiers, grouping, arrays or ordered objects."""
        token = self.peek()
        if token is None:
            raise ValueError("syntax error: unexpected end")
        self.position += 1
        if token.kind == "number":
            return Literal(number_value(float(token.value)))
        text = str(token.value)
        if token.kind == "string":
            return Literal(text)
        if token.kind == "ident":
            if text in ("true", "false", "null", "undefined"):
                return Literal(
                    {"true": True, "false": False, "null": None, "undefined": None}[text]
                )
            return Ident(text, token.offset)
        if text == "(":
            expression = self.ternary()
            self.expect(")")
            return expression
        if text == "[":
            items: list[Expr] = []
            while not self.eat("]"):
                items.append(self.ternary())
                if not self.eat(","):
                    self.expect("]")
                    break
            return Array(items)
        if text == "{":
            entries: list[tuple[str, Expr]] = []
            while not self.eat("}"):
                key = self.peek()
                if key is None or key.kind not in ("ident", "string"):
                    raise ValueError("syntax error: expected a key")
                self.position += 1
                self.expect(":")
                entries.append((str(key.value), self.ternary()))
                if not self.eat(","):
                    self.expect("}")
                    break
            return Object(entries)
        raise ValueError(f"syntax error: unexpected `{text}`")


def eval_expr(ctx: Context, expr: Expr) -> JsonValue:
    """Evaluate an AST without executing Python code or exposing function calls."""
    if isinstance(expr, Literal):
        return deepcopy(expr.value)
    if isinstance(expr, Ident):
        try:
            return ctx.view(expr.name)
        except Exception as error:
            raise Fault(error, expr.offset) from error
    if isinstance(expr, Array):
        return [eval_expr(ctx, item) for item in expr.items]
    if isinstance(expr, Object):
        return {key: eval_expr(ctx, value) for key, value in expr.entries}
    if isinstance(expr, Member):
        object = eval_expr(ctx, expr.object)
        if object is None:
            if expr.optional:
                return None
            raise ValueError(
                f"cannot read properties of null (reading {stringify(eval_expr(ctx, expr.key))})"
            )
        return member(object, eval_expr(ctx, expr.key))
    if isinstance(expr, Unary):
        value = eval_expr(ctx, expr.operand)
        if expr.operator == "!":
            return not truthy(value)
        number = to_number(value)
        return number_value(-number if expr.operator == "-" else number)
    if isinstance(expr, Binary):
        left = eval_expr(ctx, expr.left)
        if expr.operator == "&&":
            return eval_expr(ctx, expr.right) if truthy(left) else left
        if expr.operator == "||":
            return left if truthy(left) else eval_expr(ctx, expr.right)
        if expr.operator == "??":
            return eval_expr(ctx, expr.right) if left is None else left
        return binary(expr.operator, left, eval_expr(ctx, expr.right))
    return eval_expr(ctx, expr.then if truthy(eval_expr(ctx, expr.condition)) else expr.otherwise)


def member(object: JsonValue, key: JsonValue) -> JsonValue:
    """Read object properties or array/string scalar indices, returning null for misses."""
    if isinstance(object, dict):
        return deepcopy(object.get(stringify(key)))
    if isinstance(object, (list, str)):
        if key == "length":
            return len(object)
        number = to_number(key)
        if math.isfinite(number) and number >= 0 and number % 1 == 0 and number < len(object):
            return deepcopy(object[int(number)])
    return None


def binary(operator: str, left: JsonValue, right: JsonValue) -> JsonValue:
    """Apply source numeric coercions, IEEE exceptional arithmetic and equality rules."""
    if operator == "+" and (isinstance(left, str) or isinstance(right, str)):
        return stringify(left) + stringify(right)
    if operator in ("===", "!==", "==", "!="):
        equal = (
            strict_equal(left, right) if operator in ("===", "!==") else loose_equal(left, right)
        )
        return not equal if operator in ("!==", "!=") else equal
    a, b = to_number(left), to_number(right)
    if operator in ("<", ">", "<=", ">="):
        if isinstance(left, str) and isinstance(right, str):
            order = (left > right) - (left < right)
        elif math.isnan(a) or math.isnan(b):
            return False
        else:
            order = (a > b) - (a < b)
        return {"<": order < 0, ">": order > 0, "<=": order <= 0, ">=": order >= 0}[operator]
    if operator == "+":
        return number_value(a + b)
    if operator == "-":
        return number_value(a - b)
    if operator == "*":
        return number_value(a * b)
    if operator == "/":
        return number_value(a / b) if b != 0 else None
    if operator == "%":
        return number_value(math.fmod(a, b)) if b != 0 and math.isfinite(a) else None
    raise ValueError(f"unsupported operator {operator}")


def strict_equal(left: JsonValue, right: JsonValue) -> bool:
    """Compare numeric values across representations and never equate arrays or objects."""
    if type(left) in (int, float) and type(right) in (int, float):
        return to_number(left) == to_number(right)
    return not isinstance(left, (list, dict)) and json_equal(left, right)


def loose_equal(left: JsonValue, right: JsonValue) -> bool:
    """Coerce unlike scalar string/boolean/number variants, preserving null semantics."""
    scalar = (int, float, str, bool)
    left_kind = "number" if type(left) in (int, float) else type(left)
    right_kind = "number" if type(right) in (int, float) else type(right)
    if type(left) in scalar and type(right) in scalar and left_kind != right_kind:
        return to_number(left) == to_number(right)
    return strict_equal(left, right)


def truthy(value: JsonValue) -> bool:
    """Use the source JSON truthiness, where empty arrays and objects remain true."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0 and not math.isnan(value)
    if isinstance(value, str):
        return bool(value)
    return True


def to_number(value: JsonValue) -> float:
    """Convert source JSON scalars without Python-only numeric syntax or container coercions."""
    if value is None:
        return 0.0
    if isinstance(value, (bool, int, float)):
        return float(value)
    if isinstance(value, str):
        start, end = 0, len(value)
        while start < end and character_in(value[start], WHITESPACE):
            start += 1
        while end > start and character_in(value[end - 1], WHITESPACE):
            end -= 1
        text = value[start:end]
        if not text:
            return 0.0
        if FLOAT_PATTERN.fullmatch(text):
            return float(text)
    return math.nan


def stringify(value: JsonValue) -> str:
    """Render source JavaScript-like scalar and collection strings, including i64 saturation."""
    if value is None:
        return "null"
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        number = float(value)
        if number % 1 == 0 and abs(number) < 1e21:
            return str(max(-(1 << 63), min((1 << 63) - 1, int(number))))
        return format(Decimal(repr(number)), "f")
    if isinstance(value, list):
        return ",".join(stringify(item) for item in value)
    return "[object Object]"


def number_value(number: float) -> JsonValue:
    """Keep small integral numbers as JSON integers and nonfinite results as null."""
    if not math.isfinite(number):
        return None
    if number % 1 == 0 and abs(number) < 9e15:
        return int(number)
    return number
