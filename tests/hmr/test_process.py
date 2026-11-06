"""Source rust_guest.rs process cases using actual compiled original Rust guests."""

from pathlib import Path
from typing import ClassVar

from pykardia.hmr.process import ProcessSource
from tests.hmr.guest_common import GuestCase
from tests.parity.rust_guest.build import Kind


class ProcessTests(GuestCase):
    """Verify actual process callbacks, errors, recovery and cleanup."""

    kind: ClassVar[Kind] = "process"
    Source = ProcessSource

    async def test_runs_a_process_plugin(self) -> None:
        """The original guest provides views, logs, mutable calls and recoverable guest errors."""
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
            guest.call("hello", [1]),
            {
                "version": "v1",
                "calls": 1,
                "request": {"key": "greeter", "method": "hello", "args": [1]},
            },
        )
        self.assertEqual(
            guest.call("hello", None),
            {
                "version": "v1",
                "calls": 2,
                "request": {"key": "greeter", "method": "hello", "args": None},
            },
        )
        with self.assertRaisesRegex(ValueError, "^greeter refuses$"):
            guest.call("fail", None)
        with self.assertRaisesRegex(ValueError, "^the guest panicked: greeter panics$"):
            guest.call("panic", None)
        result = guest.call("hello", None)
        assert isinstance(result, dict)
        self.assertEqual(result["calls"], 5)
        self.assertIn("greeter v1 applied", [log.text for log in self.root.log_buffer()])

    async def test_a_process_guest_calls_the_host(self) -> None:
        """Host views use declared access and same-instance invocation fails without deadlocking."""
        await self.load({})
        guest = self.service()
        self.assertEqual(guest.call("view", "greeter"), self.root.view("greeter"))
        with self.assertRaisesRegex(Exception, "missing"):
            guest.call("view", "missing")
        with self.assertRaisesRegex(ValueError, "^re-entrant call into a guest instance$"):
            guest.call("invoke", {"key": "greeter", "method": "hello", "args": None})

    async def test_a_process_that_breaks_the_protocol_fails_its_later_calls(self) -> None:
        """A protocol violation poisons the instance before buffered results can be reused."""
        await self.load({})
        guest = self.service()
        with self.assertRaisesRegex(ValueError, "^invalid message from guest"):
            guest.call("print", None)
        with self.assertRaisesRegex(ValueError, "^the guest process exited$"):
            guest.call("hello", None)

    async def test_reloads_a_replaced_executable_by_respawning_it(self) -> None:
        """A replaced executable keeps the fiber identity but starts a fresh process and state."""
        entry = await self.load({})
        inner = entry.inner_fiber()
        old = self.service().call("pid", None)
        self.install("v2")
        result = await self.reload()
        self.assertEqual(result.settled_fibers, [inner])
        self.assertIs(entry.inner_fiber(), inner)
        guest = self.service()
        view = self.root.view("greeter")
        assert isinstance(view, dict)
        self.assertEqual(view["version"], "v2")
        self.assertNotEqual(guest.call("pid", None), old)
        hello = guest.call("hello", None)
        assert isinstance(hello, dict)
        self.assertEqual(hello["calls"], 2)
        self.assertIn("greeter v1 disposed", [log.text for log in self.root.log_buffer()])

    async def test_disposing_the_fiber_ends_the_process(self) -> None:
        """Removing the entry logs disposal, invalidates services and reaps the child."""
        entry = await self.load({})
        guest = self.service()
        pid = guest.call("pid", None)
        self.loader.remove(entry.id())
        await self.loader.settled()
        self.assertIn("greeter v1 disposed", [log.text for log in self.root.log_buffer()])
        with self.assertRaisesRegex(ValueError, "disposed"):
            guest.call("hello", None)
        if Path("/proc").exists():
            self.assertFalse(Path(f"/proc/{pid}").exists())

    async def test_a_crashing_process_fails_only_its_own_calls(self) -> None:
        """An exiting guest fails subsequent calls while the host can remove and load it again."""
        entry = await self.load({})
        guest = self.service()
        for method in ["exit", "hello"]:
            with self.assertRaisesRegex(ValueError, "^the guest process exited$"):
                guest.call(method, None)
        self.loader.remove(entry.id())
        await self.loader.settled()
        await self.load({})
        result = self.service().call("hello", None)
        assert isinstance(result, dict)
        self.assertEqual(result["calls"], 1)
