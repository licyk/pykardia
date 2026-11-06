"""Rendering, configuration and real stdout integration for the console plugin."""

from __future__ import annotations

import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from unittest.mock import patch

from pykardia import AggregateError, Context, Error, FiberId, Level, Message, decode
from pykardia.logger_console import (
    Align,
    ConsoleConfig,
    ConsoleExporter,
    ConsolePlugin,
    LabelStyle,
    color,
    format_duration,
    time_template,
)
from pykardia.logger_console.exporter import detect_colors


def message(level: Level, text: str, ts: int) -> Message:
    """Create the same fixed logger record used by the Rust integration tests."""
    return Message(1, ts, "test", level, text, FiberId.ROOT)


class RenderTests(unittest.TestCase):
    """Verify exact formatting and real exporter routing through core loggers."""

    def test_renders_level_name_text_and_diff(self) -> None:
        """The first diff uses creation time; subsequent records use the previous timestamp."""
        exporter = ConsoleExporter(ConsoleConfig(colors=0, show_diff=True, show_time=""))
        self.assertTrue(
            exporter.render(message(Level.Error, "message", 1000)).startswith("[E] test message +")
        )
        self.assertEqual(
            exporter.render(message(Level.Info, "{ foo: 'bar' }", 1002)),
            "[I] test { foo: 'bar' } +2ms",
        )

    def test_format_error_and_log_levels(self) -> None:
        """Actual stdout receives flattened aggregate errors and only admitted levels."""
        ctx = Context()
        handle = ctx.add_exporter(ConsoleExporter(ConsoleConfig(colors=False, show_time="")))
        logger = ctx.logger_named("test")
        output = io.StringIO()
        with redirect_stdout(output):
            logger.debug("foo bar")
            logger.with_level(Level.Debug).debug("foo bar")
            logger.log_error(Error.new(AggregateError("outer", [Error.msg("message")])))
        self.assertEqual(output.getvalue(), "[D] test foo bar\n[E] test message\n")
        handle.dispose()

    def test_label_style(self) -> None:
        """Right labels use the configured width and margin for multiline indentation."""
        exporter = ConsoleExporter(
            ConsoleConfig(
                colors=0,
                show_diff=True,
                show_time="",
                label=LabelStyle(10, 2, Align.Right),
            )
        )
        exporter.render(message(Level.Info, "warm up", 5))
        self.assertEqual(
            exporter.render(message(Level.Info, "message\nmessage", 5)),
            "      test  [I]  message\n                 message +0ms",
        )

    def test_colors_boolean_and_levels(self) -> None:
        """Boolean color flags remain distinct from numeric levels when decoding JSON."""
        for selected, expected in ((True, True), (False, False), (0, False)):
            config = decode({"colors": selected, "showTime": ""}, ConsoleConfig)
            self.assertEqual(
                "\x1b" in ConsoleExporter(config).render(message(Level.Info, "text", 1000)),
                expected,
            )
        config = decode({"levels": {"default": 1, "test": 3}, "colors": False}, ConsoleConfig)
        exporter = ConsoleExporter(config)
        self.assertEqual(exporter.level("test"), Level.Debug)
        self.assertEqual(exporter.level("other"), Level.Warn)
        self.assertIsNone(ConsoleExporter(ConsoleConfig()).level("other"))

    def test_colors_and_durations(self) -> None:
        """Use source literal outputs for ANSI escapes and duration rounding."""
        self.assertEqual(color(0, 3, "x", ";1"), "x")
        self.assertEqual(color(1, 3, "x", ";1"), "\x1b[33mx\x1b[0m")
        self.assertEqual(color(2, 200, "x", ";1"), "\x1b[38;5;200;1mx\x1b[0m")
        self.assertEqual(
            [format_duration(ms) for ms in (999, 1500, 90000, 10800000, -1500)],
            ["999ms", "2s", "2m", "3h", "-2s"],
        )

    def test_defaults_aliases_unknown_fields_and_snapshot(self) -> None:
        """Configuration follows source field names and exporter ownership is by value."""
        self.assertEqual(decode(None, ConsoleConfig), ConsoleConfig())
        config = decode(
            {"colors": 0, "maxLength": 2, "showTime": "", "label": {"margin": 0}, "future": True},
            ConsoleConfig,
        )
        exporter = ConsoleExporter(config)
        config.max_length = 100
        self.assertEqual(
            exporter.render(message(Level.Info, "你🙂好\r\nabcdef\n", 0)),
            "[I]test你🙂...\n   ab...\n   ",
        )

    def test_color_detection_with_real_terminal(self) -> None:
        """Inspect an actual PTY while changing only the environment boundary inputs."""
        if sys.platform == "win32":
            self.skipTest("requires a native POSIX pseudo-terminal")
        master, slave = os.openpty()
        try:
            with os.fdopen(slave, "w") as terminal:
                with patch.dict(os.environ, {}, clear=True), redirect_stdout(terminal):
                    self.assertEqual(detect_colors(), 1)
                    os.environ["TERM"] = "xterm-256color"
                    self.assertEqual(detect_colors(), 2)
                    os.environ["COLORTERM"] = "truecolor"
                    self.assertEqual(detect_colors(), 3)
                    os.environ["COLORTERM"] = "24bit"
                    self.assertEqual(detect_colors(), 3)
                    os.environ["NO_COLOR"] = ""
                    self.assertEqual(detect_colors(), 0)
                    forced = ConsoleExporter(ConsoleConfig(colors=True, show_time=""))
                    self.assertIn("\x1b[", forced.render(message(Level.Info, "text", 0)))
            with redirect_stdout(io.StringIO()), patch.dict(os.environ, {"COLORTERM": "truecolor"}):
                self.assertEqual(detect_colors(), 0)
        finally:
            os.close(master)

    def test_time_template_uses_one_local_snapshot(self) -> None:
        """Repeated tokens agree, with literal percent signs and Unicode left untouched."""
        before = datetime.now()
        rendered = time_template("yyyy-MM-dd hh:mm:ss.SSS|yyyy yy|%Y|你好")
        after = datetime.now()
        timestamp, year, literal, unicode = rendered.split("|")
        parsed = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S.%f")
        self.assertGreaterEqual(
            parsed, before.replace(microsecond=before.microsecond // 1000 * 1000)
        )
        self.assertLessEqual(parsed, after)
        self.assertEqual(year, f"{parsed.year:04d} {parsed.year % 100:02d}")
        self.assertEqual((literal, unicode), ("%Y", "你好"))


class ConsolePluginTests(unittest.IsolatedAsyncioTestCase):
    """Check the plugin's actual exporter lifetime in the reactive runtime."""

    async def test_plugin_registers_the_exporter_as_an_effect(self) -> None:
        """Retiring the plugin removes its stdout sink immediately after cleanup."""
        root = Context()
        fiber = root.plugin_value(ConsolePlugin, {"colors": False, "showTime": ""})
        await fiber.settled()
        self.assertEqual(fiber.effect_count(), 1)
        output = io.StringIO()
        with redirect_stdout(output):
            root.logger_named("test").info("visible")
            await fiber.dispose()
            root.logger_named("test").info("hidden")
        self.assertEqual(output.getvalue(), "[I] test visible\n")
        await root.fiber().dispose()
