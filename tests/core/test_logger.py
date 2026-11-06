"""Reference logger behavior needed by setup and cleanup error reporting."""

import asyncio
import unittest

from pykardia import (
    AggregateError,
    Context,
    EffectScope,
    Error,
    Exporter,
    Level,
    Message,
    hyphenate,
)


class RecordingExporter(Exporter):
    """A real in-memory sink whose callback can re-enter its context."""

    def __init__(self, root: Context, threshold: Level | None = None) -> None:
        """Retain emitted messages and the configured sink threshold."""
        self.root: Context = root
        self.messages: list[Message] = []
        self.threshold: Level | None = threshold

    def level(self, name: str) -> Level | None:
        """Use the sink's explicit threshold or defer to the logger."""
        return self.threshold

    def export(self, message: Message) -> None:
        """Read the real history during export to verify callbacks run outside its lock."""
        self.root.log_buffer()
        self.messages.append(message)


class LoggerTests(unittest.IsolatedAsyncioTestCase):
    """Check levels, metadata, snapshots and actual effect-owned exporter cleanup."""

    async def test_hyphenates(self) -> None:
        """Match the reference ASCII-only name normalization, including Unicode boundaries."""
        cases = {
            "fooBar": "foo-bar",
            "Qux": "qux",
            "foo:driver": "foo:driver",
            "snake_case": "snake-case",
            "HTTPServer": "httpserver",
            "ÄbcD": "Äbc-d",
        }
        for source, expected in cases.items():
            self.assertEqual(hyphenate(source), expected)

    async def test_levels_buffer_and_exporter_disposal(self) -> None:
        """Every call gets a sequence number; sinks and the buffer filter independently."""
        root = Context()
        exporter = RecordingExporter(root, Level.Debug)
        handle = root.add_exporter(exporter)
        logger = root.logger_named("exactName")
        logger.debug("debug")
        logger.info("info")
        self.assertEqual([message.sn for message in exporter.messages], [1, 2])
        self.assertEqual([message.text for message in root.log_buffer()], ["info"])
        self.assertEqual(exporter.messages[1], root.log_buffer()[0])
        self.assertEqual(exporter.messages[0].name, "exactName")
        root.set_log_buffer_size(1)
        logger.warn("warning")
        self.assertEqual([message.text for message in root.log_buffer()], ["warning"])
        root.set_log_buffer_size(0)
        logger.error("error")
        self.assertEqual(root.log_buffer(), [])
        await handle.dispose()
        logger.info("removed")
        self.assertEqual(len(exporter.messages), 4)

    async def test_zero_threshold_malformed_meta_and_aggregate_errors(self) -> None:
        """Error-level zero is meaningful; malformed metadata defaults as a whole."""
        root = Context()
        context = root.intercept("logger", {"name": "custom", "level": 0})
        logger = context.logger()
        logger.info("filtered")
        logger.log_error(AggregateError("multiple", [Error.msg("a"), ValueError("b")]))
        self.assertEqual([message.text for message in root.log_buffer()], ["a", "b"])
        self.assertEqual(logger.name(), "custom")
        self.assertEqual(logger.level(), Level.Error)
        malformed = context.intercept("logger", {"level": True}).logger()
        self.assertEqual(malformed.name(), "root")
        self.assertIsNone(malformed.level())
        logger.log_error(AggregateError("empty", []))
        self.assertEqual(root.log_buffer()[-1].text, "empty")
        with self.assertRaisesRegex(ValueError, "invalid log level 4"):
            Level(4)

    async def test_async_failure_resolves_current_logger_overlay(self) -> None:
        """A logger override changed during setup is observed when the failure is reported."""
        root = Context()
        context = root.extend()
        overlay = context.overlay()
        assert overlay is not None
        overlay.set_intercepts({"logger": {"name": "old"}})
        gate = asyncio.Event()

        async def body(scope: EffectScope) -> None:
            """Fail only after the caller changes the shared overlay."""
            await gate.wait()
            raise ValueError("failed")

        handle = context.effect("delayed", body)
        overlay.set_intercepts({"logger": {"name": "new"}})
        gate.set()
        with self.assertRaisesRegex(ValueError, "failed"):
            await handle.ready()
        self.assertEqual(root.log_buffer()[0].name, "new")
        await handle.dispose()

    async def test_chronological_buffer_names_and_exporter_identity(self) -> None:
        """Port bounded chronology, three naming paths and independently owned exporters."""
        root = Context()
        first, second = RecordingExporter(root, Level.Debug), RecordingExporter(root)
        handle = root.add_exporter(first)
        root.add_exporter(second)
        root.set_log_buffer_size(3)
        root.logger().info("one")
        root.logger_named("custom").info("two")
        root.intercept("logger", {"name": "intercepted"}).logger().info("three")
        root.logger().info("four")
        self.assertEqual([message.text for message in root.log_buffer()], ["two", "three", "four"])
        self.assertEqual(
            [message.name for message in first.messages[:3]], ["root", "custom", "intercepted"]
        )
        root.logger().debug("default hidden")
        root.intercept("logger", {"level": 3}).logger().debug("verbose")
        self.assertEqual([message.text for message in second.messages][-1], "verbose")
        self.assertNotIn("default hidden", [message.text for message in second.messages])
        await handle.dispose()
        count = len(first.messages)
        root.logger().info("after first removed")
        self.assertEqual(len(first.messages), count)
        self.assertEqual(second.messages[-1].text, "after first removed")
        await root.fiber().dispose()
