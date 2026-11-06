"""Ordered watch filters with ancestor decisions and later reinclusion."""

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from pykardia.fs.glob import Glob

Predicate = Callable[[str, bool], bool]
Filter = list[str] | Predicate


@dataclass
class Rule:
    """One compiled inclusion or exclusion and its directory traversal base."""

    negated: bool
    matcher: Glob | None
    base: str
    upward: bool = True

    def matches(self, path: str) -> bool:
        """Match the watched-directory marker or this rule's glob."""
        return self.matcher is None or self.matcher.matches(path)

    def matches_upward(self, path: str) -> bool:
        """Try the entry and each nonempty ancestor path."""
        while path:
            if self.matches(path):
                return True
            path = path.rpartition("/")[0]
        return False


def compile(patterns: Iterable[str]) -> Predicate:
    """Compile source patterns; a leading exclamation mark excludes matching entries."""
    rules = [rule(pattern) for pattern in patterns]
    for entry in reversed(rules):
        if not entry.negated:
            break
        entry.upward = False

    def keep(path: str, is_dir: bool) -> bool:
        """Apply the last decision and preserve directories leading to later inclusions."""
        decided = None
        for index, entry in enumerate(rules):
            if entry.matches_upward(path) if entry.upward else entry.matches(path):
                decided = index
        if decided is not None and not rules[decided].negated:
            return True
        after = 0 if decided is None else decided + 1
        return is_dir and any(
            not entry.negated and (contains(entry.base, path) or contains(path, entry.base))
            for entry in rules[after:]
        )

    return keep


def rule(entry: str) -> Rule:
    """Validate one relative pattern and calculate its literal directory prefix."""
    negated = entry.startswith("!")
    raw = entry[1:] if negated else entry
    segments = [part for part in raw.split("/") if part and part != "."]
    pattern = "/".join(segments) or "."
    if raw.startswith("/") or ".." in segments:
        raise ValueError(
            f"filter entry {json.dumps(entry, ensure_ascii=False)}"
            " leads outside the watched directory"
        )
    parts = pattern.split("/")
    base: list[str] = []
    for part in parts:
        if any(char in part for char in "*?[{"):
            break
        base.append(part)
    if len(base) == len(parts):
        base = base[:-1]
    return Rule(negated, None if pattern == "." else Glob(pattern), "/".join(base))


def admits(keep: Predicate, path: str, is_dir: bool) -> bool:
    """Require the entry and every traversed ancestor directory to be kept."""
    for index, char in enumerate(path):
        if char == "/" and not keep(path[:index], True):
            return False
    return keep(path, is_dir)


def contains(base: str, path: str) -> bool:
    """Compare directory ancestry at path-segment boundaries."""
    return not base or path == base or path.startswith(base + "/")
