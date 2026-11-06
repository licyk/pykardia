"""Run the Python SDK in actual subprocesses through the production process host."""

import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

import async_timeout

from pykardia import Context, JsonValue
from pykardia.guest import Meta
from pykardia.guest.export import export_process
from pykardia.hmr import Exports, GuestService, ModuleCode, ModuleGraph, ModuleSource, ProcessSource


class WithData(ModuleSource):
    """Export a real dependency value while delegating guest modules to their actual backend."""

    def __init__(self, source: ModuleSource) -> None:
        """Retain the production backend for executable guest modules."""
        self.source = source

    def load(self, id: str) -> ModuleCode:
        """Supply the declared fixture value or load an actual guest artifact."""
        if Path(id).name == "data":
            return ModuleCode.new([], lambda _: Exports.value({"text": "依赖"}))
        return self.source.load(id)


class ProcessSdkTests(unittest.IsolatedAsyncioTestCase):
    """Verify typed SDK configuration, host callbacks and per-instance state over real pipes."""

    kind: ClassVar[str] = "process"

    async def asyncSetUp(self) -> None:
        """Create an executable Python guest and separate roots for independent instances."""
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "counter"
        self.build_guest("Counter", self.path)
        self.graph = ModuleGraph(WithData(ProcessSource()))
        self.root = Context()
        self.second = Context()

    def build_guest(self, guest: str, output: Path) -> None:
        """Export the selected fixture class through the production process builder."""
        export_process(Path(__file__).with_name("counter.py"), guest, output)

    async def asyncTearDown(self) -> None:
        """Dispose both roots and reap their actual SDK processes."""
        async with async_timeout.timeout(8):
            await self.root.fiber().dispose()
            await self.second.fiber().dispose()
        self.temp.cleanup()

    async def start(self, root: Context, config: JsonValue) -> GuestService:
        """Load a real SDK process as a plugin and return its provided service."""
        definition = self.graph.import_(str(self.path)).plugin_def
        assert definition is not None
        await root.plugin_def(definition, config).settled()
        return root.get_as("counter", GuestService).arc()

    async def test_typed_config_independent_instances_and_host_callbacks(self) -> None:
        """One module creates independent processes with typed configs and real callback access."""
        first = await self.start(self.root, {"initial": 10, "unknown": "ignored"})
        second = await self.start(self.second, {"initial": 100})
        self.assertEqual(first.call("increment", 2), 12)
        self.assertEqual(first.call("increment", 3), 15)
        self.assertEqual(second.call("increment", 1), 101)
        self.assertEqual(first.call("view", "counter"), {"initial": 10, "imports": {}})
        self.assertIn("Python guest applied 你好", [log.text for log in self.root.log_buffer()])
        with self.assertRaisesRegex(ValueError, "re-entrant"):
            first.call("invoke", {"key": "counter", "method": "increment", "args": 1})
        with self.assertRaisesRegex(ValueError, "missing"):
            first.call("view", "missing")

    async def test_default_config_errors_and_application_prints(self) -> None:
        """Null uses typed defaults; errors and application prints leave the protocol usable."""
        guest = await self.start(self.root, None)
        self.assertEqual(guest.call("increment", 1), 1)
        for method, message in [
            ("fail", "counter refuses"),
            ("raise", "the guest panicked: Python exception 你好"),
            ("exit", "the guest panicked: guest requested exit"),
            ("unknown", "counter has no method unknown"),
        ]:
            with self.assertRaisesRegex(ValueError, f"^{message}$"):
                guest.call(method, None)
        self.assertEqual(guest.call("print", None), 1)
        self.assertEqual(guest.call("increment", 1), 2)
        await self.root.fiber().dispose()
        self.assertIn("Python guest disposed", [log.text for log in self.root.log_buffer()])
        with self.assertRaisesRegex(ValueError, "disposed"):
            guest.call("increment", 1)

    async def test_successful_cross_guest_calls_imports_and_null_views(self) -> None:
        """Exercise all callbacks through two exported Python guests and actual graph imports."""
        counter = await self.start(self.root, {"initial": 5})
        self.root.provide_serialized_as("nothing", None)
        path = Path(self.temp.name) / "proxy"
        self.build_guest("Proxy", path)
        definition = self.graph.import_(str(path)).plugin_def
        assert definition is not None
        await self.root.plugin_def(definition, {"initial": 2}).settled()
        proxy = self.root.get_as("proxy", GuestService).arc()
        self.assertEqual(counter.call("increment", 0), 7)
        self.assertEqual(proxy.value(), {"imports": {str(path.parent / "data"): {"text": "依赖"}}})
        self.assertEqual(
            proxy.call("invoke", {"key": "counter", "method": "increment", "args": 3}), 10
        )
        self.assertEqual(proxy.call("view", "counter"), {"initial": 5, "imports": {}})
        if self.kind == "process":
            with self.assertRaisesRegex(
                ValueError, "^EOF while parsing a value at line 1 column 0$"
            ):
                proxy.call("view", "nothing")
        else:
            self.assertIsNone(proxy.call("view", "nothing"))

    async def test_metadata_builders_preserve_explicit_null_and_unnamed_plugins(self) -> None:
        """Builder output has the original protocol fields, including optional and null cases."""
        self.assertEqual(Meta().to_json(), {"deps": []})
        self.assertEqual(Meta.value(None).to_json(), {"deps": [], "value": None})
        self.assertEqual(
            Meta().dep("./data").inject("settings").provide("counter").to_json(),
            {
                "deps": ["./data"],
                "plugin": {"name": None, "inject": ["settings"], "provide": ["counter"]},
            },
        )
