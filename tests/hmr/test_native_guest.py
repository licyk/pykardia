"""Source rust_guest.rs native-library and compiled WASM acceptance."""

import os
from typing import ClassVar

from pykardia.hmr.dylib import DylibSource
from pykardia.hmr.wasm import WasmSource
from tests.hmr.guest_common import GuestCase
from tests.parity.rust_guest.build import Kind, artifact


class DylibTests(GuestCase):
    """Exercise original Rust guest libraries through the actual ctypes C ABI."""

    kind: ClassVar[Kind] = "dylib"
    Source = DylibSource

    async def test_loads_a_dylib_exporting_only_the_kardia_names(self) -> None:
        """A library without the cordis_* symbols loads through its kardia_* ones."""
        os.link(artifact("dylib", "kardia-names"), self.path)
        await self.load({})
        view = self.root.view("greeter")
        assert isinstance(view, dict)
        self.assertEqual(view["version"], "v1")
        result = self.service().call("hello", None)
        assert isinstance(result, dict)
        self.assertEqual(result["calls"], 1)

    async def test_runs_a_dylib_plugin(self) -> None:
        """The C ABI carries real metadata, application buffers, calls and logs."""
        await self.load({"name": "alice"})
        self.assertEqual(
            self.root.view("greeter"),
            {
                "version": "v1",
                "input": {"config": {"name": "alice"}, "imports": {}},
            },
        )
        guest = self.service()
        self.assertEqual(
            guest.call("hello", None),
            {
                "version": "v1",
                "calls": 1,
                "request": {"key": "greeter", "method": "hello", "args": None},
            },
        )
        second = guest.call("hello", "你好")
        assert isinstance(second, dict)
        self.assertEqual(second["calls"], 2)
        self.assertEqual(second["request"], {"key": "greeter", "method": "hello", "args": "你好"})
        self.assertIn("greeter v1 applied", [log.text for log in self.root.log_buffer()])

    async def test_reports_guest_errors_and_panics_of_calls(self) -> None:
        """A contained Rust panic and ordinary error both leave the native instance callable."""
        await self.load({})
        guest = self.service()
        with self.assertRaisesRegex(ValueError, "^greeter refuses$"):
            guest.call("fail", None)
        with self.assertRaisesRegex(ValueError, "^the guest panicked: greeter panics$"):
            guest.call("panic", None)
        result = guest.call("hello", None)
        assert isinstance(result, dict)
        self.assertEqual(result["calls"], 3)

    async def test_reloads_a_rebuilt_library_in_place(self) -> None:
        """A unique copied library loads new code while replacement preserves the fiber."""
        entry = await self.load({})
        inner = entry.inner_fiber()
        old = self.service()
        old.call("hello", None)
        self.install("v2")
        result = await self.reload()
        self.assertEqual(result.settled_fibers, [inner])
        self.assertIs(entry.inner_fiber(), inner)
        view = self.root.view("greeter")
        assert isinstance(view, dict)
        self.assertEqual(view["version"], "v2")
        hello = self.service().call("hello", None)
        assert isinstance(hello, dict)
        self.assertEqual(hello["calls"], 1)
        self.assertIn("greeter v1 disposed", [log.text for log in self.root.log_buffer()])
        with self.assertRaisesRegex(ValueError, "disposed"):
            old.call("hello", None)

    async def test_disposing_the_fiber_frees_the_guest_state(self) -> None:
        """Guest dispose releases native state and retained service handles become unusable."""
        entry = await self.load({})
        guest = self.service()
        self.loader.remove(entry.id())
        await self.loader.settled()
        self.assertIn("greeter v1 disposed", [log.text for log in self.root.log_buffer()])
        with self.assertRaisesRegex(ValueError, "disposed"):
            guest.call("hello", None)

    async def test_a_dylib_guest_calls_the_host(self) -> None:
        """C callbacks can read their own view and reject a reentrant invocation."""
        await self.load({})
        guest = self.service()
        self.assertEqual(guest.call("view", "greeter"), self.root.view("greeter"))
        with self.assertRaisesRegex(ValueError, "missing"):
            guest.call("view", "missing")
        with self.assertRaisesRegex(ValueError, "^re-entrant call into a guest instance$"):
            guest.call("invoke", {"key": "greeter", "method": "hello", "args": None})


class WasmRustTests(GuestCase):
    """Load the identical original guest source compiled to wasm32-unknown-unknown."""

    kind: ClassVar[Kind] = "wasm"
    Source = WasmSource

    async def test_runs_the_same_guest_as_a_wasm_module(self) -> None:
        """The compiled Rust module runs, calls, fails, reloads and disposes through Wasmtime."""
        entry = await self.load({"name": "alice"})
        self.assertEqual(
            self.root.view("greeter"),
            {
                "version": "v1",
                "input": {"config": {"name": "alice"}, "imports": {}},
            },
        )
        guest = self.service()
        result = guest.call("hello", None)
        assert isinstance(result, dict)
        self.assertEqual(result["calls"], 1)
        with self.assertRaisesRegex(ValueError, "^greeter refuses$"):
            guest.call("fail", None)
        self.install("v2")
        await self.reload()
        view = self.root.view("greeter")
        assert isinstance(view, dict)
        self.assertEqual(view["version"], "v2")
        self.loader.remove(entry.id())
        await self.loader.settled()
        self.assertIn("greeter v2 disposed", [log.text for log in self.root.log_buffer()])
