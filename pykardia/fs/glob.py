"""Byte-oriented glob matching corresponding to globset's configured source semantics."""

import re


class Glob:
    """Compile literal separators, recursive stars, classes and nested alternatives."""

    def __init__(self, pattern: str) -> None:
        """Parse a source glob once and compile its byte regular expression."""
        self.pattern = pattern
        self.index = 0
        self.branches: list[list[bytes]] = [[]]
        self.alternates: list[int] = []
        while self.index < len(pattern):
            char = pattern[self.index]
            self.index += 1
            if char == "*":
                self.star()
            elif char == "?":
                self.branches[-1].append(b"[^/]")
            elif char == "[":
                self.branches[-1].append(self.character_class())
            elif char == "{":
                self.alternates.append(len(self.branches))
                self.branches.append([])
            elif char == "}":
                if not self.alternates:
                    raise self.error(
                        "unopened alternate group; missing '{' (maybe escape '}' with '[}]'?)"
                    )
                start = self.alternates.pop()
                parts = [b"".join(branch) for branch in self.branches[start:] if branch]
                del self.branches[start:]
                self.branches[-1].append(b"(?:" + b"|".join(parts) + b")" if parts else b"")
            elif char == "," and self.alternates:
                self.branches.append([])
            else:
                if char == "\\":
                    if self.index == len(pattern):
                        raise self.error("dangling '\\'")
                    char = pattern[self.index]
                    self.index += 1
                self.branches[-1].append(re.escape(char.encode()))
        if self.alternates:
            raise self.error("unclosed alternate group; missing '}' (maybe escape '{' with '[{]'?)")
        tokens = self.branches[0]
        expression = b".*" if tokens == [b"(?:/?|.*/)"] else b"".join(tokens)
        self.regex = re.compile(expression, re.DOTALL)

    def matches(self, path: str) -> bool:
        """Match the whole encoded path, with question marks consuming one byte."""
        return self.regex.fullmatch(path.encode(errors="surrogateescape")) is not None

    def error(self, message: str) -> ValueError:
        """Attach the source's stable glob diagnostic prefix."""
        return ValueError(f"error parsing glob '{self.pattern}': {message}")

    def star(self) -> None:
        """Recognize recursive stars only at the source parser's component boundaries."""
        tokens = self.branches[-1]
        previous = self.pattern[self.index - 2] if self.index >= 2 else None
        if self.index >= len(self.pattern) or self.pattern[self.index] != "*":
            tokens.append(b"[^/]*")
            return
        self.index += 1
        following = self.pattern[self.index] if self.index < len(self.pattern) else None
        if not tokens:
            if following not in (None, "/"):
                tokens.extend([b"[^/]*", b"[^/]*"])
            else:
                tokens.append(b"(?:/?|.*/)")
                if following is not None:
                    self.index += 1
            return
        if previous != "/" and (len(self.branches) <= 1 or previous not in (",", "{")):
            tokens.extend([b"[^/]*", b"[^/]*"])
            return
        suffix = following is None or (following in (",", "}") and len(self.branches) >= 2)
        if following == "/":
            self.index += 1
        elif not suffix:
            tokens.extend([b"[^/]*", b"[^/]*"])
            return
        last = tokens.pop()
        tokens.append(
            last if last in (b"(?:/?|.*/)", b"/.*") else b"/.*" if suffix else b"(?:/|/.*/)"
        )

    def character_class(self) -> bytes:
        """Parse source character ranges, preserving literal first brackets and hyphens."""
        negated = self.index < len(self.pattern) and self.pattern[self.index] in "!^"
        if negated:
            self.index += 1
        ranges: list[tuple[str, str]] = []
        first, in_range = True, False
        while self.index < len(self.pattern):
            char = self.pattern[self.index]
            self.index += 1
            if char == "]" and not first:
                break
            if char == "-" and not first and not in_range:
                in_range = True
            elif in_range:
                start = ranges[-1][0]
                if char < start:
                    raise self.error(f"invalid range; '{start}' > '{char}'")
                ranges[-1] = (start, char)
                in_range = False
            else:
                ranges.append((char, char))
            first = False
        else:
            raise self.error("unclosed character class; missing ']'")
        if in_range:
            ranges.append(("-", "-"))
        parts = [
            re.escape(a.encode()) + (b"-" + re.escape(b.encode()) if a != b else b"")
            for a, b in ranges
        ]
        return b"[" + (b"^" if negated else b"") + b"".join(parts) + b"]"
