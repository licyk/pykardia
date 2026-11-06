"""Blocking JSON Lines guest processes, one independent child per instance."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from pykardia.core.json import dumps, loads
from pykardia.core.value import JsonValue
from pykardia.hmr.graph import ModuleCode, ModuleSource
from pykardia.hmr.guest import Code, Export, Host, Instance, module


class ProcessSource(ModuleSource):
    """Load executable paths as guest modules speaking the existing process protocol."""

    def load(self, id: str) -> ModuleCode:
        """Read a fresh metadata process and return code that starts one child per fiber."""
        return module(id, ProcessCode(id))


class ProcessCode(Code):
    """An executable path, resolved by the OS again when each instance starts."""

    def __init__(self, path: str) -> None:
        """Retain the module's executable path without shell interpolation."""
        self.path = path

    def instantiate(self, host: Host) -> ProcessInstance:
        """Start a child with dedicated protocol pipes and inherited stderr."""
        command = [self.path]
        directory: tempfile.TemporaryDirectory[str] | None = None
        try:
            if sys.platform == "win32":
                if zipfile.is_zipfile(self.path):
                    with open(self.path, "rb") as archive:
                        interpreter = archive.readline().decode("utf-8").removeprefix("#!").strip()
                    command = [interpreter, self.path]
                else:
                    # Windows locks running executables; a private copy allows live replacement.
                    directory = tempfile.TemporaryDirectory(prefix="pykardia-process-")
                    executable = Path(directory.name) / Path(self.path).name
                    shutil.copy2(self.path, executable)
                    command = [str(executable)]
            child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        except OSError as error:
            if directory is not None:
                directory.cleanup()
            raise ValueError(f"cannot start guest {self.path}: {error}") from error
        return ProcessInstance(child, host, directory)


class ProcessInstance(Instance):
    """A child transport that becomes permanently broken after an invalid exchange."""

    def __init__(
        self,
        child: subprocess.Popen[bytes],
        host: Host,
        directory: tempfile.TemporaryDirectory[str] | None = None,
    ) -> None:
        """Retain both pipes and instance buffers until explicit disposal."""
        self.child = child
        assert child.stdin is not None and child.stdout is not None
        self.stdin = child.stdin
        self.stdout = child.stdout
        self.host = host
        self.broken = False
        self.directory = directory

    def call(self, export: Export) -> int:
        """Exchange one export, poisoning and killing the process on transport failure."""
        if self.broken:
            raise ValueError("the guest process exited")
        try:
            return self.exchange(export)
        except Exception:
            self.broken = True
            try:
                self.child.kill()
            except OSError:
                pass
            raise

    def exchange(self, export: Export) -> int:
        """Handle callback messages until one done record terminates the export."""
        input = loads(self.host.message) if self.host.message else None
        self.send({"export": export.value, "input": input})
        while True:
            message = self.receive()
            obj = message if isinstance(message, dict) else {}
            if "done" in obj:
                return i32(obj["done"], 1)
            if "output" in obj:
                self.host.write(text(obj, "output"))
                continue
            call = obj.get("call")
            if call == "log":
                self.host.log(i32(obj.get("level"), 2), text(obj, "text"))
                continue
            if call == "provide":
                status = self.host.provide(text(obj, "key"), dumps(obj.get("value")))
            elif call == "view":
                status = self.host.view(text(obj, "key"))
            elif call == "invoke":
                status = self.host.invoke(
                    text(obj, "key"), text(obj, "method"), dumps(obj.get("args"))
                )
            else:
                raise ValueError(f"unexpected message from guest: {dumps(message).decode('utf-8')}")
            reply: dict[str, JsonValue]
            if status < 0:
                reply = {"error": self.host.message.decode("utf-8", errors="replace")}
            else:
                try:
                    result = loads(self.host.message)
                except ValueError:
                    result = None
                reply = {"result": result}
            self.send(reply)

    def send(self, message: JsonValue) -> None:
        """Write and flush one UTF-8 JSON record or report the generic process-exit error."""
        try:
            self.stdin.write(dumps(message) + b"\n")
            self.stdin.flush()
        except OSError as error:
            raise ValueError("the guest process exited") from error

    def receive(self) -> JsonValue:
        """Read exactly one protocol line and reject malformed JSON before another export."""
        try:
            line = self.stdout.readline()
            line.decode("utf-8")
        except (OSError, UnicodeError) as error:
            raise ValueError("the guest process exited") from error
        if not line:
            raise ValueError("the guest process exited")
        try:
            return loads(line)
        except ValueError as error:
            raise ValueError(f"invalid message from guest: {error}") from error

    def close(self) -> None:
        """Kill, reap and close protocol pipes so no disposed process remains running."""
        try:
            self.child.kill()
        except OSError:
            pass
        self.child.wait()
        self.stdin.close()
        self.stdout.close()
        if self.directory is not None:
            self.directory.cleanup()


def text(message: dict[str, JsonValue], key: str) -> bytes:
    """Read a protocol string field, using the source's empty-string fallback."""
    value = message.get(key)
    return value.encode("utf-8") if isinstance(value, str) else b""


def i32(value: JsonValue, default: int) -> int:
    """Read a JSON i64 and apply the ABI's wrapping signed 32-bit cast."""
    number = value if type(value) is int and -(2**63) <= value < 2**63 else default
    return (number + 2**31) % 2**32 - 2**31
