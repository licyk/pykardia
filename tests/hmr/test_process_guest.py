"""Process guests: spawners, caller metadata, deadlines, cancellation, exits and protocol 1."""

import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path

import async_timeout

from pykardia import Context, Fiber, FiberState, JsonValue
from pykardia.hmr import (
    ExitCause,
    GuestCommand,
    GuestExited,
    GuestHandle,
    GuestMeta,
    GuestProcess,
    GuestService,
    GuestStarted,
    ModuleGraph,
    ProcessGuest,
    ProcessSource,
    Spawner,
    StdSpawner,
)
from tests.parity.rust_guest.build import artifact


class Recorder(Spawner):
    """Record what is spawned and add a variable to the environment."""

    def __init__(self) -> None:
        """Start with no commands."""
        self.commands: list[GuestCommand] = []

    def spawn(self, command: GuestCommand) -> GuestProcess:
        """Record the command and delegate to the default spawner."""
        self.commands.append(command)
        return StdSpawner().spawn(command.with_env("SPAWNED_BY", "recorder"))


def greeter_meta() -> GuestMeta:
    """Return the Rust fixture's metadata."""
    return GuestMeta("greeter", [], ["greeter"])


async def failed(fiber: Fiber) -> str:
    """Wait for the exit watcher to fail the fiber and return its error."""
    async with async_timeout.timeout(5):
        while fiber.error() is None:
            await asyncio.sleep(0.01)
    return str(fiber.error())


class ProcessGuestTests(unittest.IsolatedAsyncioTestCase):
    """Drive the compiled Rust fixture through ProcessGuest."""

    @classmethod
    def setUpClass(cls) -> None:
        """Build the fixture's process guests before the asynchronous tests."""
        artifact("process")
        artifact("v1")

    async def asyncSetUp(self) -> None:
        """Create a private copy of the executable and a root."""
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)
        self.path = self.dir / artifact("process").name
        os.link(artifact("process"), self.path)
        self.root = Context()

    async def asyncTearDown(self) -> None:
        """Dispose the root and reap every child."""
        async with async_timeout.timeout(8):
            await self.root.fiber().dispose()
        self.temp.cleanup()

    def guest(self, timeout: float | None = None, frame_limit: int = 1 << 24) -> ProcessGuest:
        """Return a process guest for the fixture."""
        return ProcessGuest(GuestCommand(str(self.path)), timeout=timeout, frame_limit=frame_limit)

    async def start(self, guest: ProcessGuest) -> tuple[Fiber, GuestService]:
        """Start the guest's plugin with caller metadata and return its service."""
        fiber = self.root.plugin_def(guest.plugin(greeter_meta()), {})
        await fiber.settled()
        return fiber, self.root.get_as("greeter", GuestService).arc()

    async def test_spawns_the_command_through_the_spawner_only_when_a_fiber_starts(self) -> None:
        """Arguments, environment and directory reach the process; building spawns nothing."""
        recorder = Recorder()
        command = (
            GuestCommand(str(self.path))
            .with_args("--flag", "two words")
            .with_env("GREETING", "hi")
            .with_cwd(self.dir)
        )
        definition = ProcessGuest(command, recorder).plugin(greeter_meta())
        self.assertEqual(recorder.commands, [])
        await self.root.plugin_def(definition, {}).settled()
        greeter = self.root.get_as("greeter", GuestService).arc()
        self.assertEqual(greeter.call("args", None), ["--flag", "two words"])
        self.assertEqual(greeter.call("env", "GREETING"), "hi")
        self.assertEqual(greeter.call("env", "SPAWNED_BY"), "recorder")
        cwd = greeter.call("cwd", None)
        assert isinstance(cwd, str)
        self.assertEqual(Path(cwd).resolve(), self.dir.resolve())
        self.assertEqual(len(recorder.commands), 1)
        handle = greeter.handle()
        assert handle is not None
        self.assertEqual(handle.protocol(), 2)

    async def test_fails_a_guest_whose_metadata_differs_from_its_definition(self) -> None:
        """The guest's own metadata must match the caller's."""
        guest = ProcessGuest(GuestCommand(str(self.path)).with_env("GREETER_NAME", "impostor"))
        fiber = self.root.plugin_def(guest.plugin(greeter_meta()), {})
        with self.assertRaisesRegex(ValueError, "impostor"):
            await fiber.settled()
        self.assertEqual(fiber.state(), FiberState.Failed)

    async def test_a_call_past_its_deadline_kills_the_guest_and_fails_its_fiber(self) -> None:
        """A hung call kills the process, reports its exit and fails the fiber until update."""
        exits: list[tuple[Fiber, ExitCause]] = []
        self.root.on(GuestExited, lambda event: exits.append((event.fiber, event.exit.cause)))
        fiber, greeter = await self.start(self.guest())
        handle = greeter.handle()
        assert handle is not None
        with self.assertRaisesRegex(ValueError, "^the guest did not answer in time$"):
            await asyncio.to_thread(greeter.call_timeout, "hang", None, 0.2)
        exit = handle.exit()
        assert exit is not None
        self.assertEqual(
            (exit.cause, exit.reason), (ExitCause.KILLED, "the guest did not answer in time")
        )
        with self.assertRaisesRegex(ValueError, "^the guest process exited$"):
            greeter.call("hello", None)
        self.assertFalse(greeter.is_available())
        await failed(fiber)
        self.assertEqual(exits, [(fiber, ExitCause.KILLED)])
        await fiber.update({})
        again = self.root.get_as("greeter", GuestService).arc()
        result = again.call("hello", None)
        assert isinstance(result, dict)
        self.assertEqual(result["calls"], 1)

    async def test_the_default_timeout_bounds_calls_without_a_deadline(self) -> None:
        """ProcessGuest.timeout applies when the caller sets none."""
        _, greeter = await self.start(self.guest(timeout=0.3))
        self.assertEqual(await greeter.call_async("sleep", 10), "slept")
        with self.assertRaisesRegex(ValueError, "^the guest did not answer in time$"):
            await greeter.call_async("hang", None)

    async def test_cancelling_an_async_call_tells_the_guest(self) -> None:
        """A cancelled call ends early in the guest, which keeps running."""
        _, greeter = await self.start(self.guest())
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(greeter.call_async("sleep", 10_000), 0.2)
        self.assertEqual(await greeter.call_async("cancelled", None, 5), 1)
        self.assertTrue(greeter.is_available())

    async def test_answers_pings_while_a_call_runs(self) -> None:
        """A ping is answered while the guest sleeps in a call."""
        _, greeter = await self.start(self.guest())
        handle = greeter.handle()
        assert handle is not None
        call = asyncio.ensure_future(greeter.call_async("sleep", 1000))
        await asyncio.sleep(0.1)
        self.assertLess(await handle.ping(5), 0.8)
        self.assertFalse(call.done())
        self.assertEqual(await call, "slept")

    async def test_reports_a_guest_that_exits_with_its_stderr_tail(self) -> None:
        """An exit is reported with its status and stderr, and fails the fiber."""
        started: list[GuestHandle] = []
        self.root.on(GuestStarted, lambda event: started.append(event.handle))
        fiber, greeter = await self.start(self.guest())
        handle = started.pop()
        own = greeter.handle()
        assert own is not None
        self.assertEqual(handle.id(), own.id())
        greeter.call("exit_later", None)
        async with async_timeout.timeout(5):
            exit = await handle.exited()
        self.assertEqual((exit.cause, exit.status), (ExitCause.EXITED, 3))
        tail = exit.stderr_tail.decode()
        self.assertIn("greeter v1 started", tail)
        self.assertIn("greeter v1 dying", tail)
        self.assertEqual(await failed(fiber), str(exit))
        self.assertFalse(greeter.is_available())
        with self.assertRaisesRegex(ValueError, "^the guest process exited$"):
            greeter.call("hello", None)

    async def test_reports_a_killed_guest_with_its_stderr_tail(self) -> None:
        """A host kill is reported as such, with the stderr written before it."""
        fiber, greeter = await self.start(self.guest())
        greeter.call("stderr", "last words")
        handle = greeter.handle()
        assert handle is not None
        handle.kill()
        async with async_timeout.timeout(5):
            exit = await handle.exited()
        self.assertEqual((exit.cause, exit.reason), (ExitCause.KILLED, "killed by the host"))
        self.assertNotEqual(exit.status, 0)
        self.assertIn(b"last words", exit.stderr_tail)
        await failed(fiber)

    async def test_a_message_over_the_frame_limit_kills_the_guest(self) -> None:
        """A line longer than the limit kills the guest."""
        _, greeter = await self.start(self.guest(frame_limit=4096))
        self.assertEqual(greeter.call("big", 100), "x" * 100)
        message = "a message from the guest exceeds the limit of 4096 bytes"
        with self.assertRaisesRegex(ValueError, f"^{message}$"):
            greeter.call("big", 10_000)
        handle = greeter.handle()
        assert handle is not None
        exit = handle.exit()
        assert exit is not None
        self.assertEqual((exit.cause, exit.reason), (ExitCause.KILLED, message))

    async def test_disposing_reports_a_disposed_guest_without_failing_its_fiber(self) -> None:
        """Disposal is reported as such and emits no exit event."""
        exits: list[GuestExited] = []
        self.root.on(GuestExited, exits.append)
        fiber, greeter = await self.start(self.guest())
        handle = greeter.handle()
        assert handle is not None
        await fiber.dispose()
        exit = handle.exit()
        assert exit is not None
        self.assertEqual(exit.cause, ExitCause.DISPOSED)
        self.assertEqual(exits, [])
        with self.assertRaisesRegex(ValueError, "^the guest instance is disposed$"):
            greeter.call("hello", None)

    async def test_serves_a_guest_speaking_protocol_version_1(self) -> None:
        """A hand-written version 1 guest answers the handshake with status 1 and runs."""
        path = self.dir / artifact("v1").name
        os.link(artifact("v1"), path)
        definition = ModuleGraph(ProcessSource()).import_(str(path)).plugin_def
        assert definition is not None
        fiber = self.root.plugin_def(definition, {})
        await fiber.settled()
        self.assertEqual(self.root.view("legacy"), {"protocol": 1})
        legacy = self.root.get_as("legacy", GuestService).arc()
        request: JsonValue = {"key": "legacy", "method": "echo", "args": [1]}
        self.assertEqual(await legacy.call_async("echo", [1], 5), request)
        handle = legacy.handle()
        assert handle is not None
        self.assertEqual(handle.protocol(), 1)
        with self.assertRaisesRegex(ValueError, "kardia-guest/2"):
            await handle.ping(1)
        start = time.monotonic()
        await fiber.dispose()
        self.assertLess(time.monotonic() - start, 5)
        exit = handle.exit()
        assert exit is not None
        self.assertEqual(exit.cause, ExitCause.DISPOSED)
