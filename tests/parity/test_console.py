"""Compare Python console rendering directly with captured Rust output."""

import json
import unittest
from dataclasses import dataclass
from pathlib import Path

from pykardia import FiberId, JsonValue, Level, Message, decode
from pykardia.logger_console import (
    ConsoleConfig,
    ConsoleExporter,
    color,
    format_duration,
    name_color,
)


@dataclass
class RenderInput:
    """One fixed rendering scenario supplied to both production implementations."""

    config: ConsoleConfig
    name: str
    text: str
    ts: int


@dataclass
class ConsoleInput:
    """Shared data for the Rust and Python thin observation drivers."""

    names: list[str]
    durations: list[int]
    render: list[RenderInput]


class ConsoleParityTests(unittest.TestCase):
    """Require exact text and ANSI parity without stripping or normalizing output."""

    def test_rust_reference(self) -> None:
        """Compare 35 color records, 24 durations and six multiline formatted messages."""
        directory = Path(__file__).parent / "console"
        inputs = decode(json.loads((directory / "input.json").read_text()), ConsoleInput)
        expected = json.loads((directory / "reference.json").read_text())
        colors: list[JsonValue] = []
        for name in inputs.names:
            for level in (0, 1, 2, 3, 255):
                code = name_color(name, level)
                colors.append([code, color(level, code, name, ";1")])
        rendered: list[JsonValue] = []
        for row in inputs.render:
            exporter = ConsoleExporter(row.config)
            exporter.render(Message(1, 1000, row.name, Level.Info, "warm up", FiberId.ROOT))
            rendered.append(
                exporter.render(Message(1, row.ts, row.name, Level.Info, row.text, FiberId.ROOT))
            )
        self.assertEqual(
            {
                "colors": colors,
                "durations": [format_duration(ms) for ms in inputs.durations],
                "render": rendered,
            },
            expected,
        )
