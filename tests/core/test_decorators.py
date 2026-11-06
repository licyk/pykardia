"""Run macro-equivalent declarations and document Python declaration/access error stages."""

from __future__ import annotations

import unittest
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import cast

from pykardia import (
    Context,
    Deps,
    FiberState,
    Inject,
    Injected,
    PluginDef,
    Service,
    Undeclared,
    plugin,
    service,
)
from pykardia.decorators import declaration_name


@service(name="database")
@dataclass
class Database(Service):
    """An explicitly named service holding a configuration-derived URL."""

    url: str


@service
class TaskQueue(Service):
    """A service whose name derives from its class identifier."""


@dataclass
class DbConfig:
    """Typed source configuration of the database plugin."""

    url: str


@plugin(provide=["database"])
async def db_plugin(ctx: Context, config: DbConfig) -> None:
    """Provide a typed service from the actual decoded plugin configuration."""
    ctx.provide(Database(config.url))


@plugin(name="greeter")
async def greeter(ctx: Injected[tuple[Database, TaskQueue]]) -> None:
    """Access declared dependencies and log through the captured plugin context."""
    database = ctx.get(Database)
    _, queue = ctx.deps()
    ctx.context().logger().info(f"{database.arc().url} {queue.key()}")


class DecoratorTests(unittest.IsolatedAsyncioTestCase):
    """Port derive/declaration and actual plugin execution cases plus UI error equivalents."""

    def test_derives_names_and_declarations(self) -> None:
        """Preserve explicit names and the macro's distinct acronym hyphenation algorithm."""
        self.assertEqual(Database.NAME, "database")
        self.assertEqual(TaskQueue.NAME, "task-queue")
        self.assertEqual(db_plugin.plugin_name(), "db-plugin")
        self.assertEqual(db_plugin.provide_spec(), ("database",))
        self.assertEqual(greeter.plugin_name(), "greeter")
        self.assertEqual(list(greeter.inject_spec().keys()), ["database", "task-queue"])
        self.assertEqual(declaration_name("HTTPServer"), "h-t-t-p-server")
        self.assertEqual(Inject.of(Database, TaskQueue), Inject(["database", "task-queue"]))
        self.assertEqual(Deps(Database, TaskQueue).keys(), ["database", "task-queue"])

    async def test_generated_plugins_run(self) -> None:
        """A decorated typed provider activates the real decorated dependent plugin."""
        root = Context()
        root.provide(TaskQueue())
        consumer = root.plugin_def(greeter)
        await consumer.settled()
        self.assertEqual(consumer.state(), FiberState.Pending)
        provider = root.plugin_def(db_plugin, {"url": "postgres://"})
        await provider.settled()
        await consumer.settled()
        self.assertEqual(consumer.state(), FiberState.Active)
        self.assertEqual(
            [message.text for message in root.log_buffer()], ["postgres:// task-queue"]
        )
        self.assertEqual(root.deps(Database, TaskQueue)[0].key(), "database")
        self.assertEqual(Deps().get(root), ())
        await root.fiber().dispose()

    def test_not_async_is_rejected_at_declaration(self) -> None:
        """Map not_async.rs to a Python decorator-time declaration error."""

        def invalid(ctx: Context) -> None:
            """Intentionally violate only the required async declaration contract."""

        with self.assertRaisesRegex(TypeError, "must be async"):
            plugin(cast(Callable[..., Awaitable[None]], invalid))

    def test_untyped_config_is_rejected_at_declaration(self) -> None:
        """Python shared references replace Arc; a missing config type is the boundary error."""

        async def invalid(ctx: Context, config: DbConfig) -> None:
            """Supply a declaration whose runtime annotation will deliberately be removed."""

        invalid.__annotations__.pop("config")
        with self.assertRaisesRegex(TypeError, "configuration parameter must have a type"):
            plugin(invalid)

    async def test_undeclared_typed_get_fails_at_access(self) -> None:
        """Map undeclared.rs to a runtime typed-access error instead of a Rust compiler error."""

        @plugin
        async def invalid(ctx: Injected[tuple[Database]]) -> None:
            """Deliberately access a service outside the declared type tuple."""
            ctx.get(TaskQueue)

        root = Context()
        root.provide(Database("postgres://"))
        root.provide(TaskQueue())
        fiber = root.plugin_def(invalid)
        with self.assertRaises(Undeclared):
            await fiber.settled()
        self.assertEqual(fiber.state(), FiberState.Failed)
        await root.fiber().dispose()

    def test_builder_keeps_decorated_definition_identity(self) -> None:
        """Decorated functions yield a reusable PluginDef rather than implicit named globals."""
        self.assertIsInstance(db_plugin, PluginDef)
        self.assertEqual(db_plugin.key(), db_plugin.name("alias").key())
