"""Render console messages and export them to standard output."""

import os
import sys
import threading
import time
from copy import deepcopy

from pykardia.core.logger import Exporter, Level, Message
from pykardia.logger_console.config import Align, ConsoleConfig
from pykardia.logger_console.format import color, format_duration, name_color, time_template


class ConsoleExporter(Exporter):
    """A configured stdout sink with a shared previous-message timestamp."""

    def __init__(self, config: ConsoleConfig) -> None:
        """Snapshot configuration and detect colors once at exporter creation."""
        self._config = deepcopy(config)
        selected = config.colors
        if selected is None:
            self._colors = detect_colors()
        elif isinstance(selected, bool):
            self._colors = max(detect_colors(), 1) if selected else 0
        else:
            self._colors = selected
        self._timestamp = max(0, time.time_ns() // 1_000_000)
        self._lock = threading.Lock()

    def render(self, message: Message) -> str:
        """Render one message, advancing the diff timestamp even when diff is hidden."""
        config = self._config
        prefix = f"[{message.level.as_str()[0].upper()}]"
        space = " " * (1 if config.label.margin is None else config.label.margin)
        indent = 3 + len(space)
        output = ""
        if config.show_time:
            indent += len(config.show_time)
            output += color(self._colors, 8, time_template(config.show_time), "")
        code = name_color(message.name, self._colors)
        label = color(self._colors, code, message.name, ";1")
        padding = " " * max(0, config.label.width - len(message.name))
        if config.label.align is Align.Right:
            output += f"{padding}{label}{space}{prefix}{space}"
            indent += config.label.width + len(space)
        else:
            output += f"{prefix}{space}{label}{padding}{space}"
        output += truncate_lines(message.text, config.max_length).replace("\n", "\n" + " " * indent)
        with self._lock:
            if config.show_diff:
                diff = signed_i64(message.ts) - signed_i64(self._timestamp)
                output += color(self._colors, code, f" +{format_duration(diff)}", "")
            self._timestamp = message.ts
        return output

    def export(self, message: Message) -> None:
        """Print exactly one rendered record followed by a newline to stdout."""
        print(self.render(message), flush=True)

    def level(self, name: str) -> Level | None:
        """Resolve a named threshold before the optional default threshold."""
        return self._config.levels.get(name, self._config.levels.get("default"))


def truncate_lines(text: str, max_length: int) -> str:
    """Strip one trailing CR per line and truncate Unicode scalar values independently."""
    result: list[str] = []
    for line in text.split("\n"):
        line = line.removesuffix("\r")
        result.append(line[:max_length] + "..." if len(line) > max_length else line)
    return "\n".join(result)


def detect_colors() -> int:
    """Apply NO_COLOR, terminal presence, COLORTERM and TERM in source precedence order."""
    if "NO_COLOR" in os.environ or not sys.stdout.isatty():
        return 0
    if os.environ.get("COLORTERM", "") in ("truecolor", "24bit"):
        return 3
    return 2 if "256" in os.environ.get("TERM", "") else 1


def signed_i64(value: int) -> int:
    """Interpret a source u64 timestamp as the signed integer used for diff arithmetic."""
    return (value + (1 << 63)) % (1 << 64) - (1 << 63)
