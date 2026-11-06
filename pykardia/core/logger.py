"""Named logging, bounded history and resource-owned exporters."""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass
from enum import IntEnum
from typing import TYPE_CHECKING

from pykardia.core.error import AggregateError, Error
from pykardia.core.id_table import IdTable
from pykardia.core.key import FiberId

if TYPE_CHECKING:
    from pykardia.core.context import Context
    from pykardia.core.root import Root


class Level(IntEnum):
    """Log verbosity in the reference's numeric order."""

    Error = 0
    Warn = 1
    Info = 2
    Debug = 3

    @classmethod
    def _missing_(cls, value: object) -> None:
        """Reject an unknown level with the reference diagnostic."""
        raise ValueError(f"invalid log level {value}")

    def as_str(self) -> str:
        """Return the lowercase level label."""
        return self.name.lower()


@dataclass(frozen=True)
class Message:
    """One immutable log call, shared by every exporter that receives it."""

    sn: int
    ts: int
    name: str
    level: Level
    text: str
    fiber: FiberId


class Exporter(ABC):
    """A log sink whose lifetime is controlled by its registration effect."""

    @abstractmethod
    def export(self, message: Message) -> None:
        """Receive a message after level filtering, outside the logger lock."""

    def level(self, name: str) -> Level | None:
        """Return an optional sink-specific threshold for this logger name."""
        return None


class LoggerState:
    """Shared exporter registrations, sequence allocation and bounded message history."""

    def __init__(self) -> None:
        """Use the reference's default history limit of 1,000 messages."""
        self.lock = threading.Lock()
        self.exporters: IdTable[Exporter] = IdTable()
        self.buffer: deque[Message] = deque()
        self.buffer_size: int = 1000
        self.sn: int = 0


@dataclass
class LoggerIntercept:
    """Decoded name and verbosity overrides; malformed metadata defaults as a whole."""

    name: str | None = None
    level: Level | None = None


class Logger:
    """A named logger retaining its root and originating fiber identity."""

    def __init__(self, root: Root, name: str, level: Level | None, fiber: FiberId) -> None:
        """Bind a logger to the already-resolved name, threshold and resource owner."""
        self._root: Root = root
        self._name: str = name
        self._level: Level | None = level
        self._fiber: FiberId = fiber

    @classmethod
    def internal(cls, root: Root, name: str, fiber: FiberId) -> Logger:
        """Construct an internal logger without context interception."""
        return cls(root, hyphenate(name), None, fiber)

    @classmethod
    def for_caller(cls, caller: Context, fallback: str) -> Logger:
        """Resolve name and level from the caller's logger interception metadata."""
        meta = caller.intercept_meta("logger", LoggerIntercept)
        name = hyphenate(fallback) if meta.name is None else meta.name
        return cls(caller._root, name, meta.level, caller.fiber_id())

    def level(self) -> Level | None:
        """Return this logger's explicit verbosity, if any."""
        return self._level

    def with_level(self, level: Level) -> Logger:
        """Return a logger sharing identity and root with a new verbosity threshold."""
        return Logger(self._root, self._name, level, self._fiber)

    def name(self) -> str:
        """Return the resolved logger name."""
        return self._name

    def error(self, text: object) -> None:
        """Log a displayable error message."""
        self.log(Level.Error, text)

    def warn(self, text: object) -> None:
        """Log a displayable warning."""
        self.log(Level.Warn, text)

    def info(self, text: object) -> None:
        """Log a displayable informational message."""
        self.log(Level.Info, text)

    def debug(self, text: object) -> None:
        """Log a displayable debug message, subject to each sink's threshold."""
        self.log(Level.Debug, text)

    def log_error(self, error: BaseException) -> None:
        """Log a failure, expanding nonempty aggregate errors member by member."""
        self.log_error_at(Level.Error, error)

    def log_error_at(self, level: Level, error: BaseException) -> None:
        """Expand aggregates recursively without replacing the chosen severity."""
        cause = error.as_std() if isinstance(error, Error) else error
        if isinstance(cause, AggregateError) and cause.errors:
            for member in cause.errors:
                self.log_error_at(level, member)
        else:
            self.log(level, error)

    def log(self, level: Level, text: object) -> None:
        """Assign one sequence number, retain eligible history and call matching exporters."""
        state = self._root.logger
        with state.lock:
            state.sn += 1
            sn = state.sn
            exporters = list(state.exporters.values())
        message = Message(
            sn, max(0, time.time_ns() // 1_000_000), self._name, level, str(text), self._fiber
        )
        threshold = self._level if self._level is not None else Level.Info
        if level <= threshold:
            with state.lock:
                if state.buffer_size > 0:
                    while len(state.buffer) >= state.buffer_size:
                        state.buffer.popleft()
                    state.buffer.append(message)
        for exporter in exporters:
            override = exporter.level(self._name)
            if level <= (threshold if override is None else override):
                exporter.export(message)


def hyphenate(source: str) -> str:
    """Normalize ASCII capitals and underscores exactly as the reference logger does."""
    result: list[str] = []
    previous_upper = True
    for index, character in enumerate(source):
        if character == "_":
            result.append("-")
            previous_upper = False
        elif "A" <= character <= "Z":
            if index > 0 and not previous_upper:
                result.append("-")
            result.append(character.lower())
            previous_upper = True
        else:
            result.append(character)
            previous_upper = False
    return "".join(result)
