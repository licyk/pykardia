"""JSON host operations shared by native and WebAssembly guest exports."""

from typing import Protocol

from pykardia.core.json import dumps, loads
from pykardia.core.value import JsonValue
from pykardia.guest.api import Error, Host, Level


class RawHost(Protocol):
    """The six byte-oriented callbacks of the original guest ABI."""

    def read(self, length: int) -> bytes:
        """Copy the host's current message."""
        ...

    def write(self, data: bytes) -> None:
        """Replace the host's output."""
        ...

    def log(self, level: int, data: bytes) -> None:
        """Forward UTF-8 logging."""
        ...

    def provide(self, key: bytes, value: bytes) -> int:
        """Provide serialized data and return a result length or encoded error."""
        ...

    def view(self, key: bytes) -> int:
        """Read a service and return a result length or encoded error."""
        ...

    def invoke(self, key: bytes, method: bytes, args: bytes) -> int:
        """Invoke a service and return a result length or encoded error."""
        ...


class AbiHost(Host):
    """Adapt the source ABI's UTF-8 and negative-length error encoding."""

    def __init__(self, raw: RawHost) -> None:
        """Retain callbacks valid for the current synchronous export."""
        self.raw = raw

    def log(self, level: Level, text: str) -> None:
        """Log without involving the guest's stdout."""
        self.raw.log(int(level), text.encode("utf-8"))

    def provide(self, key: str, value: JsonValue) -> None:
        """Provide a JSON service and propagate host errors."""
        self.reply(self.raw.provide(key.encode("utf-8"), dumps(value)))

    def view(self, key: str) -> JsonValue:
        """Decode a service view, including a successful JSON null."""
        return reply_value(self.reply(self.raw.view(key.encode("utf-8"))))

    def invoke(self, key: str, method: str, args: JsonValue) -> JsonValue:
        """Invoke another guest using the unchanged byte protocol."""
        return reply_value(
            self.reply(self.raw.invoke(key.encode("utf-8"), method.encode("utf-8"), dumps(args)))
        )

    def reply(self, length: int) -> bytes:
        """Read exactly the reported buffer, translating negative lengths into errors."""
        if length < 0:
            raise Error(self.raw.read(-1 - length).decode("utf-8", errors="replace"))
        return self.raw.read(length)


def reply_value(data: bytes) -> JsonValue:
    """Convert host reply decoding failures into SDK errors instead of guest panics."""
    try:
        return loads(data)
    except ValueError as error:
        raise Error(str(error)) from error
