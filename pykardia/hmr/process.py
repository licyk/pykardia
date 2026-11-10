"""Executables as guests: one child process per instance, speaking JSON lines.

Protocol version 1 and ``kardia-guest/2`` are both spoken: the host offers version 2 as a
``hello`` export, which a version 1 guest answers with status 1.
"""

from __future__ import annotations

import os
import queue
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import IO

from pykardia.core.json import dumps, loads
from pykardia.core.plugin import PluginDef
from pykardia.core.value import JsonValue
from pykardia.hmr.exit import ExitCause, GuestHandle
from pykardia.hmr.graph import ModuleCode, ModuleSource
from pykardia.hmr.guest import (
    CallControl,
    Export,
    GuestCode,
    GuestHost,
    GuestInstance,
    GuestMeta,
    exited,
    guest_module,
    guest_plugin,
    timed_out,
)
from pykardia.hmr.pipes import Cancel, Closed, GuestChild, Message, Shared, start

PROTOCOL = "kardia-guest/2"
"""The protocol version a host offers in its handshake."""
HANDSHAKE_TIMEOUT = 10.0
FRAME_LIMIT = 16 * 1024 * 1024
DISPOSE_TIMEOUT = 5.0
"""How long ``dispose`` may take when the guest has no timeout of its own."""


@dataclass(frozen=True)
class GuestCommand:
    """A process guest's program, arguments, environment additions and working directory."""

    program: str
    args: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = ()
    cwd: str | None = None

    def with_args(self, *args: str) -> GuestCommand:
        """Return the command with more arguments."""
        return replace(self, args=(*self.args, *args))

    def with_env(self, key: str, value: str) -> GuestCommand:
        """Return the command with a variable set on top of the spawner's environment."""
        return replace(self, env=(*self.env, (key, value)))

    def with_cwd(self, cwd: str | os.PathLike[str]) -> GuestCommand:
        """Return the command with a working directory."""
        return replace(self, cwd=os.fspath(cwd))


@dataclass
class GuestProcess:
    """A started guest process: its pipes and the control its instance keeps."""

    stdin: IO[bytes]
    stdout: IO[bytes]
    child: GuestChild
    stderr: IO[bytes] | None = None

    @classmethod
    def from_child(cls, child: subprocess.Popen[bytes]) -> GuestProcess:
        """Wrap a child spawned with piped stdin and stdout (and stderr, if piped)."""
        if child.stdin is None or child.stdout is None:
            raise ValueError("the guest's stdin and stdout must be piped")
        return cls(child.stdin, child.stdout, child, child.stderr)


class Spawner(ABC):
    """Starts the processes of process guests, so a host decides how they run."""

    @abstractmethod
    def spawn(self, command: GuestCommand) -> GuestProcess:
        """Start ``command`` with piped stdin and stdout; pipe stderr for the exit report."""


class StdSpawner(Spawner):
    """Run the command with ``subprocess`` in the host's environment plus its variables."""

    def spawn(self, command: GuestCommand) -> GuestProcess:
        """Start the process; on Windows a private copy keeps the file replaceable."""
        argv = [command.program, *command.args]
        directory: tempfile.TemporaryDirectory[str] | None = None
        if sys.platform == "win32":
            if zipfile.is_zipfile(command.program):
                with open(command.program, "rb") as archive:
                    interpreter = archive.readline().decode("utf-8").removeprefix("#!").strip()
                argv = [interpreter, *argv]
            else:
                # Windows locks running executables; a private copy allows live replacement.
                directory = tempfile.TemporaryDirectory(prefix="pykardia-process-")
                executable = Path(directory.name) / Path(command.program).name
                shutil.copy2(command.program, executable)
                argv[0] = str(executable)
        env = {**os.environ, **dict(command.env)} if command.env else None
        try:
            child = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                cwd=command.cwd,
            )
        except OSError:
            if directory is not None:
                directory.cleanup()
            raise
        process = GuestProcess.from_child(child)
        if directory is not None:
            process.child = CopiedChild(child, directory)
        return process


class CopiedChild:
    """A child running from a private copy, removed once the child has been waited for."""

    def __init__(
        self, child: subprocess.Popen[bytes], directory: tempfile.TemporaryDirectory[str]
    ) -> None:
        """Retain the child and its copy's directory."""
        self.child = child
        self.directory = directory

    @property
    def pid(self) -> int:
        """Return the process id."""
        return self.child.pid

    def kill(self) -> None:
        """Kill the child."""
        self.child.kill()

    def poll(self) -> int | None:
        """Return the return code once the child has ended, then remove its copy."""
        code = self.child.poll()
        if code is not None:
            self.directory.cleanup()
        return code

    def wait(self, timeout: float | None = None) -> int:
        """Wait for the child, then remove its copy."""
        code = self.child.wait(timeout)
        self.directory.cleanup()
        return code


@dataclass
class ProcessGuest(GuestCode):
    """A guest run as a child process: one process per instance.

    A call past its deadline kills the process, as does a message that is not JSON or longer
    than the frame limit; a process that ends fails its fiber (see ``GuestExited``).
    """

    command: GuestCommand
    spawner: Spawner = field(default_factory=StdSpawner)
    timeout: float | None = None
    """How long each export may take when its caller sets no deadline (``dispose``: 5 s)."""
    handshake_timeout: float = HANDSHAKE_TIMEOUT
    frame_limit: int = FRAME_LIMIT
    """The longest line the guest may send, in bytes."""

    def module(self, id: str) -> ModuleCode:
        """Return the module the guest exports, read from its ``meta`` (starts a process)."""
        return guest_module(id, self)

    def plugin(self, meta: GuestMeta) -> PluginDef:
        """Return the plugin with caller metadata; each process must declare the same."""
        return guest_plugin(self, meta)

    def instantiate(self, host: GuestHost) -> ProcessInstance:
        """Spawn a process through the spawner and complete the handshake."""
        try:
            process = self.spawner.spawn(self.command)
        except OSError as error:
            raise ValueError(f"cannot start guest {self.command.program}: {error}") from error
        shared = start(
            process.child,
            process.stdin,
            process.stdout,
            process.stderr,
            self.frame_limit,
            host.logger(),
        )
        instance = ProcessInstance(host, shared, self.timeout)
        try:
            instance.handshake(self.handshake_timeout)
        except Exception:
            instance.close()
            raise
        return instance


class ProcessSource(ModuleSource):
    """Load executable paths as guest modules, run without arguments by ``StdSpawner``."""

    def load(self, id: str) -> ModuleCode:
        """Read a fresh metadata process and return code that starts one child per fiber."""
        return ProcessGuest(GuestCommand(id)).module(id)


class Fault(Exception):
    """An exchange cannot go on: the process is gone, or must be killed for ``kill``."""

    def __init__(self, message: str, kill: bool) -> None:
        """Record the reason and whether the guest must still be killed."""
        super().__init__(message)
        self.kill = kill


class ProcessInstance(GuestInstance):
    """A guest instance in a child process."""

    def __init__(self, host: GuestHost, shared: Shared, timeout: float | None) -> None:
        """Retain the host, the process's shared state and the default timeout."""
        self.host = host
        self.shared = shared
        self.timeout = timeout
        self.calls = 0
        self.broken = False

    def handshake(self, timeout: float) -> None:
        """Offer ``kardia-guest/2``; a version 1 guest answers with a failure status."""
        self.shared.send({"export": "hello", "input": {"protocol": PROTOCOL}})
        deadline = time.monotonic() + timeout
        try:
            while True:
                received = self.receive(deadline)
                if isinstance(received, Cancel):
                    continue
                message = received if isinstance(received, dict) else {}
                if "ready" in message:
                    if message["ready"] != PROTOCOL:
                        version = dumps(message["ready"]).decode("utf-8")
                        raise Fault(f"the guest speaks an unknown protocol: {version}", True)
                    self.shared.protocol = 2
                    return
                if "done" in message:
                    return
                if "output" not in message:
                    raise Fault(f"unexpected message from guest: {text_of(received)}", True)
        except Fault as fault:
            raise self.fault(fault) from None

    def call(self, export: Export, input: bytes, control: CallControl) -> bytes:
        """Exchange one export; a fault breaks the instance and reports its exit first."""
        if self.broken:
            if export is Export.DISPOSE:
                return b""
            raise exited()
        timeout = self.timeout
        if export is Export.DISPOSE:
            # a guest exits once it is disposed: its reader may reap it before this call returns
            self.shared.mark(ExitCause.DISPOSED)
            timeout = timeout or DISPOSE_TIMEOUT
        deadline = control.deadline()
        if deadline is None and timeout is not None:
            deadline = time.monotonic() + timeout
        self.calls += 1
        number = self.calls
        control.on_cancel(lambda: self.shared.incoming.put(Cancel(number)))
        try:
            return self.exchange(export, input, number, deadline)
        except Fault as fault:
            raise self.fault(fault) from None

    def exchange(self, export: Export, input: bytes, number: int, deadline: float | None) -> bytes:
        """Send the export and answer the guest's host calls until it is done."""
        v2 = self.shared.protocol == 2

        def tagged(message: dict[str, JsonValue]) -> dict[str, JsonValue]:
            """Add the export's id under version 2."""
            return {**message, "id": number} if v2 else message

        self.shared.send(tagged({"export": export.value, "input": loads(input) if input else None}))
        output, cancelled = b"", False
        while True:
            received = self.receive(deadline)
            if isinstance(received, Cancel):
                if received.id == number and not cancelled:
                    cancelled = True
                    if v2:
                        self.shared.send({"cancel": number})
                continue
            message = received if isinstance(received, dict) else {}
            if v2 and message.get("id") != number:
                # a host call of no running export would wait forever
                if "call" in message:
                    self.shared.send({"id": message.get("id"), "error": "no such call"})
                continue
            if "done" in message:
                status = i32(message["done"], 1)
                if cancelled:
                    raise ValueError("the guest call was cancelled")
                if status == 0:
                    return output
                if output:
                    raise ValueError(output.decode("utf-8", errors="replace"))
                raise ValueError(f"guest failed with status {status}")
            if "output" in message:
                value = message["output"]
                output = value.encode("utf-8") if isinstance(value, str) else b""
                continue
            call = message.get("call")
            reply: dict[str, JsonValue]
            try:
                if cancelled and call is not None:
                    raise ValueError("the guest call was cancelled")
                if call == "provide":
                    self.host.provide(text(message, "key"), message.get("value"))
                    result: JsonValue = None
                elif call == "view":
                    result = self.host.view(text(message, "key"))
                elif call == "invoke":
                    result = self.host.invoke(
                        text(message, "key"), text(message, "method"), message.get("args")
                    )
                else:
                    raise Fault(f"unexpected message from guest: {text_of(received)}", True)
                reply = {"result": result}
            except Fault:
                raise
            except Exception as error:
                reply = {"error": str(error)}
            self.shared.send(tagged(reply))

    def receive(self, deadline: float | None) -> JsonValue | Cancel:
        """Wait for the guest's next message, or a cancellation, until ``deadline``."""
        try:
            if deadline is None:
                item = self.shared.incoming.get()
            else:
                item = self.shared.incoming.get(timeout=max(0.0, deadline - time.monotonic()))
        except queue.Empty:
            raise Fault(str(timed_out()), True) from None
        if isinstance(item, Message):
            return item.value
        if isinstance(item, Cancel):
            return item
        assert isinstance(item, Closed)
        raise Fault(item.reason or str(exited()), False)

    def fault(self, fault: Fault) -> ValueError:
        """Break the instance; its exit report is published before the error is raised."""
        self.broken = True
        if fault.kill:
            self.shared.kill(ExitCause.KILLED, str(fault))
        self.shared.reap()
        return ValueError(str(fault))

    def handle(self) -> GuestHandle:
        """Return the handle of the instance's process."""
        return GuestHandle(self.shared)

    def close(self) -> None:
        """Kill and reap the process so no disposed instance is left running."""
        self.shared.kill(ExitCause.DISPOSED)
        self.shared.reap()


def text(message: dict[str, JsonValue], key: str) -> str:
    """Read a protocol string field, using the source's empty-string fallback."""
    value = message.get(key)
    return value if isinstance(value, str) else ""


def text_of(message: JsonValue | Cancel) -> str:
    """Render a message for a diagnostic."""
    return dumps(message if not isinstance(message, Cancel) else None).decode("utf-8")


def i32(value: JsonValue, default: int) -> int:
    """Read a JSON i64 and apply the ABI's wrapping signed 32-bit cast."""
    number = value if type(value) is int and -(2**63) <= value < 2**63 else default
    return (number + 2**31) % 2**32 - 2**31
