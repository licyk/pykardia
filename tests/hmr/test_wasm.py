"""Source wasm.rs cases with real WAT compilation and host callback execution."""

import tempfile
import unittest
from pathlib import Path

import async_timeout

from pykardia import Context, Fiber, FiberState, JsonValue, PluginDef
from pykardia.hmr import Hmr, HmrExt, ModuleGraph, ReconcileResult
from pykardia.hmr.guest import GuestService
from pykardia.hmr.wasm import WasmSource
from pykardia.loader import Loader
from tests.hmr.wat import forwarder, greeter, wat


class WasmTests(unittest.IsolatedAsyncioTestCase):
    """Observe real Wasmtime memory and the actual bridge, loader and HMR services."""

    async def asyncSetUp(self) -> None:
        """Create a private WAT source directory and production module services."""
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = Context()
        self.graph = ModuleGraph(WasmSource())
        await self.root.plugin_def(Loader.plugin(self.graph)).settled()
        await self.root.plugin_def(Hmr.plugin(self.graph)).settled()
        self.loader = self.root.get(Loader).arc()
        self.hmr = HmrExt(self.root.get(Hmr))

    async def asyncTearDown(self) -> None:
        """Release all guest stores through real fiber disposal."""
        async with async_timeout.timeout(8):
            await self.root.fiber().dispose()
        self.temp.cleanup()

    async def load(self, name: str, text: str, config: JsonValue = None) -> Fiber:
        """Compile and load a real WAT entry, retaining failed fibers for error assertions."""
        path = self.base / name
        path.write_text(text)
        await self.loader.create({"id": name, "name": str(path), "config": config})
        await self.loader.settled()
        fiber = self.loader.resolve(name).inner_fiber()
        assert fiber is not None
        return fiber

    async def reload(self, name: str) -> ReconcileResult:
        """Report one actual source path through the serialized HMR callback."""

        async def roots() -> list[Path]:
            """Return this test's changed WAT path."""
            return [self.base / name]

        return await self.hmr.reconcile(roots)

    async def test_runs_a_wasm_plugin(self) -> None:
        """Application and invocation preserve the guest's JSON and log output."""
        fiber = await self.load("greeter.wat", greeter("v1"), {"name": "alice"})
        self.assertEqual(fiber.state(), FiberState.Active)
        self.assertEqual(self.root.view("greeter"), {"config": {"name": "alice"}, "imports": {}})
        self.assertEqual(self.root.view("version"), "v1")
        service = self.root.get_as("greeter", GuestService).arc()
        self.assertEqual(
            service.call("hello", [1]), {"key": "greeter", "method": "hello", "args": [1]}
        )
        self.assertIn("greeter v1 applied", [log.text for log in self.root.log_buffer()])

    async def test_reloads_a_changed_module_in_place(self) -> None:
        """New WASM code replaces the same fiber only after the old instance disposes."""
        fiber = await self.load("greeter.wat", greeter("v1"), {})
        (self.base / "greeter.wat").write_text(greeter("v2"))
        result = await self.reload("greeter.wat")
        self.assertEqual(result.settled_fibers, [fiber])
        self.assertIs(self.loader.resolve("greeter.wat").inner_fiber(), fiber)
        self.assertEqual(self.root.view("version"), "v2")
        logs = [log.text for log in self.root.log_buffer()]
        self.assertLess(logs.index("greeter v1 disposed"), logs.index("greeter v2 applied"))

    async def test_disposing_the_fiber_releases_the_instance(self) -> None:
        """Disposal withdraws provisions and retained service handles reject subsequent calls."""
        await self.load("greeter.wat", greeter("v1"), {})
        service = self.root.get_as("greeter", GuestService).arc()
        self.loader.remove("greeter.wat")
        await self.loader.settled()
        self.assertIn("greeter v1 disposed", [log.text for log in self.root.log_buffer()])
        with self.assertRaisesRegex(Exception, "greeter"):
            self.root.view("greeter")
        with self.assertRaisesRegex(ValueError, "disposed"):
            service.call("hello", None)

    async def test_guest_failures_and_traps_fail_the_plugin(self) -> None:
        """Explicit failure text and an actual unreachable instruction fail application."""
        failing = wat(
            {"plugin": {}},
            ["no greeting today"],
            '(func (export "cordis_apply") (param i32) (result i32) '
            "(call $write @0) (i32.const 1))",
        )
        fiber = await self.load("failing.wat", failing, {})
        self.assertEqual(fiber.state(), FiberState.Failed)
        self.assertEqual(str(fiber.error()), "no greeting today")
        trapping = wat(
            {"plugin": {}},
            [],
            '(func (export "cordis_apply") (param i32) (result i32) unreachable)',
        )
        fiber = await self.load("trapping.wat", trapping, {})
        self.assertEqual(fiber.state(), FiberState.Failed)
        self.assertIn("unreachable", str(fiber.error()))

    async def test_views_declared_services_only(self) -> None:
        """A guest can read an injected provision but an undeclared sibling read fails."""

        async def provide(ctx: Context, config: JsonValue) -> None:
            """Publish a real serializable sibling provision for the guest to access."""
            ctx.provide_serialized_as("source", {"n": 1})

        await self.root.plugin_def(PluginDef.new(provide)).settled()
        declared = forwarder({"plugin": {"inject": ["source"]}}, "(call $view @0)")
        fiber = await self.load("declared.wat", declared, {})
        self.assertEqual(fiber.state(), FiberState.Active)
        self.assertEqual(self.root.view("copy"), {"n": 1})
        undeclared = forwarder({"plugin": {}}, "(call $view @0)")
        fiber = await self.load("undeclared.wat", undeclared, {})
        self.assertIn("source", str(fiber.error()))

    async def test_calls_services_of_other_guests(self) -> None:
        """One guest invokes another's service through a declared host binding."""
        await self.load("greeter.wat", greeter("v1"), {})
        caller = forwarder({"plugin": {"inject": ["greeter"]}}, "(call $invoke @1 @3 @4)")
        fiber = await self.load("caller.wat", caller, {})
        self.assertEqual(fiber.state(), FiberState.Active)
        self.assertEqual(self.root.view("copy"), {"key": "greeter", "method": "hello", "args": [2]})

    async def test_rejects_calls_back_into_a_running_guest(self) -> None:
        """A guest providing then invoking itself receives the bridge's reentry error."""
        reentrant = forwarder(
            {"plugin": {}}, "(drop (call $provide @5 @4)) (call $invoke @5 @3 @4)"
        )
        fiber = await self.load("reentrant.wat", reentrant, {})
        self.assertIn("re-entrant", str(fiber.error()))

    async def test_passes_dependency_values_and_reloads_dependents(self) -> None:
        """Relative guest dependencies resolve to paths and their JSON values trigger reload."""
        data = self.base / "data.wat"
        data.write_text(wat({"value": {"n": 1}}, [], ""))
        meta: JsonValue = {"deps": ["./data.wat"], "plugin": {"provide": ["greeter", "version"]}}
        await self.load("greeter.wat", greeter("v1", meta), {})
        self.assertEqual(
            self.root.view("greeter"), {"config": {}, "imports": {str(data): {"n": 1}}}
        )
        data.write_text(wat({"value": {"n": 2}}, [], ""))
        await self.reload("data.wat")
        self.assertEqual(
            self.root.view("greeter"), {"config": {}, "imports": {str(data): {"n": 2}}}
        )
