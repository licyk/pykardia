"""Python guest SDK process adapter: JSON lines on stdin/stdout, version 1 or kardia-guest/2."""

from __future__ import annotations

import contextlib
import queue
import sys
import threading
from typing import BinaryIO, TypeVar

from pykardia.core.json import dumps, loads
from pykardia.core.value import JsonValue
from pykardia.guest.abi import reply_value
from pykardia.guest.api import CANCELLATION, Error, Guest, Host, Level
from pykardia.guest.runtime import Runtime

C = TypeVar("C")
PROTOCOL = "kardia-guest/2"


class Channel(Host):
    """Synchronous host callbacks over one process's dedicated protocol streams."""

    def __init__(self, stdin: BinaryIO, stdout: BinaryIO) -> None:
        """Capture the real pipes before application stdout is redirected to stderr."""
        self.stdin = stdin
        self.stdout = stdout
        self.lock = threading.Lock()
        self.queue: queue.Queue[JsonValue] | None = None
        self.id: int | None = None

    def read(self) -> JsonValue:
        """Read one valid JSON line from stdin, raising EOF when it closes or is invalid."""
        line = self.stdin.readline()
        if not line:
            raise EOFError
        try:
            return loads(line)
        except ValueError as error:
            raise EOFError from error

    def receive(self) -> JsonValue:
        """Return the next message for the running export, raising EOF when the host is gone."""
        if self.queue is None:
            return self.read()
        message = self.queue.get()
        if message is EOF:
            raise EOFError
        return message

    def write(self, message: JsonValue) -> None:
        """Write and flush one protocol record through the retained stdout pipe."""
        with self.lock:
            self.stdout.write(dumps(message) + b"\n")
            self.stdout.flush()

    def send(self, message: dict[str, JsonValue]) -> None:
        """Write a message of the running export, with its id under kardia-guest/2."""
        self.write(message if self.id is None else {**message, "id": self.id})

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

    def request(self, message: dict[str, JsonValue]) -> bytes:
        """Exchange one callback and preserve the original process SDK's null-to-empty rule."""
        self.send(message)
        response = self.receive()
        obj = response if isinstance(response, dict) else {}
        if "error" in obj:
            error = obj["error"]
            raise Error(error if isinstance(error, str) else "")
        result = obj.get("result")
        return dumps(result) if result is not None else b""

    def listen(self) -> None:
        """Read stdin for kardia-guest/2: answer pings, record cancellations, queue the rest."""
        assert self.queue is not None
        try:
            while True:
                message = self.read()
                obj = message if isinstance(message, dict) else {}
                cancel = obj.get("cancel")
                if "ping" in obj:
                    self.write({"pong": obj["ping"]})
                elif isinstance(cancel, int) and not isinstance(cancel, bool):
                    CANCELLATION.cancelled = cancel
                else:
                    self.queue.put(message)
        except (EOFError, OSError, ValueError):
            self.queue.put(EOF)


EOF: JsonValue = {"__eof__": True}
"""Queued when stdin closes; compared by identity."""


def serve(guest: type[Guest[C]]) -> None:
    """Serve one guest until dispose or EOF, keeping application prints off protocol stdout.

    A host offering kardia-guest/2 gets it: a thread then reads stdin, so pings are answered
    while an export runs and a cancelled export sees ``Host.cancelled``. Any other host is
    served protocol version 1.
    """
    channel = Channel(sys.stdin.buffer, sys.stdout.buffer)
    runtime = Runtime(guest)
    with contextlib.redirect_stdout(sys.stderr):
        try:
            first: JsonValue = channel.read()
        except EOFError:
            return
        obj = first if isinstance(first, dict) else {}
        input = obj.get("input")
        if obj.get("export") == "hello" and isinstance(input, dict):
            if input.get("protocol") == PROTOCOL:
                channel.queue = queue.Queue()
                threading.Thread(target=channel.listen, daemon=True).start()
                channel.write({"ready": PROTOCOL})
                first = None
        request: JsonValue = first
        while True:
            if request is None:
                try:
                    request = channel.receive()
                except EOFError:
                    return
            obj = request if isinstance(request, dict) else {}
            request = None
            identity = obj.get("id")
            channel.id = identity if type(identity) is int else None
            CANCELLATION.running = channel.id or 0
            export = obj.get("export")
            export = export if isinstance(export, str) else ""
            value = obj.get("input")
            input_bytes = dumps(value) if value is not None else b""
            status, output = runtime.dispatch(export, channel, input_bytes)
            if output is not None:
                channel.send({"output": output.decode("utf-8", errors="replace")})
            channel.send({"done": status})
            CANCELLATION.running = 0
            if export == "dispose":
                return
