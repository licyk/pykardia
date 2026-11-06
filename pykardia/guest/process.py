"""Python guest SDK process adapter for the original stdin/stdout JSON Lines ABI."""

from __future__ import annotations

import contextlib
import sys
from typing import BinaryIO, TypeVar

from pykardia.core.json import dumps, loads
from pykardia.core.value import JsonValue
from pykardia.guest.abi import reply_value
from pykardia.guest.api import Error, Guest, Host, Level
from pykardia.guest.runtime import Runtime

C = TypeVar("C")


class Channel(Host):
    """Synchronous host callbacks over one process's dedicated protocol streams."""

    def __init__(self, stdin: BinaryIO, stdout: BinaryIO) -> None:
        """Capture the real pipes before application stdout is redirected to stderr."""
        self.stdin = stdin
        self.stdout = stdout

    def receive(self) -> JsonValue:
        """Read one valid JSON line, raising EOF when the host closes or sends invalid input."""
        line = self.stdin.readline()
        if not line:
            raise EOFError
        try:
            return loads(line)
        except ValueError as error:
            raise EOFError from error

    def send(self, message: JsonValue) -> None:
        """Write and flush one protocol record through the retained stdout pipe."""
        self.stdout.write(dumps(message) + b"\n")
        self.stdout.flush()

    def log(self, level: Level, text: str) -> None:
        """Send a host log callback without expecting a reply."""
        self.send({"call": "log", "level": int(level), "text": text})

    def provide(self, key: str, value: JsonValue) -> None:
        """Publish one service and consume the host's acknowledgement."""
        self.request({"call": "provide", "key": key, "value": value})

    def view(self, key: str) -> JsonValue:
        """Read the host's service view using the original SDK's empty-null reply behavior."""
        return reply_value(self.request({"call": "view", "key": key}))

    def invoke(self, key: str, method: str, args: JsonValue) -> JsonValue:
        """Invoke another guest and decode the host's callback reply."""
        return reply_value(
            self.request({"call": "invoke", "key": key, "method": method, "args": args})
        )

    def request(self, message: JsonValue) -> bytes:
        """Exchange one callback and preserve the original process SDK's null-to-empty rule."""
        self.send(message)
        response = self.receive()
        obj = response if isinstance(response, dict) else {}
        if "error" in obj:
            error = obj["error"]
            raise Error(error if isinstance(error, str) else "")
        result = obj.get("result")
        return dumps(result) if result is not None else b""


def serve(guest: type[Guest[C]]) -> None:
    """Serve one guest until dispose or EOF, keeping application prints off protocol stdout."""
    channel = Channel(sys.stdin.buffer, sys.stdout.buffer)
    runtime = Runtime(guest)
    with contextlib.redirect_stdout(sys.stderr):
        while True:
            try:
                request = channel.receive()
            except EOFError:
                return
            obj = request if isinstance(request, dict) else {}
            export = obj.get("export")
            export = export if isinstance(export, str) else ""
            value = obj.get("input")
            input = dumps(value) if value is not None else b""
            status, output = runtime.dispatch(export, channel, input)
            if output is not None:
                channel.send({"output": output.decode("utf-8", errors="replace")})
            channel.send({"done": status})
            if export == "dispose":
                return
