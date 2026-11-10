# SPDX-License-Identifier: MIT OR Apache-2.0
"""Guest implementation interface and synchronous host operations."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import IntEnum
from typing import Generic, TypeVar

from typing_extensions import Self, TypeForm

from pykardia.core.value import JsonValue
from pykardia.guest.meta import Meta

C = TypeVar("C")


class Error(Exception):
    """A guest error whose message is sent to the host unchanged."""

    @classmethod
    def msg(cls, message: object) -> Error:
        """Create a guest error from a displayable value."""
        return cls(str(message))

    def message(self) -> str:
        """Return the protocol error text."""
        return str(self)


class Level(IntEnum):
    """Guest log levels encoded by the original integer ABI."""

    Error = 0
    Warn = 1
    Info = 2
    Debug = 3


@dataclass
class Request:
    """A service method request received by one guest instance."""

    key: str
    method: str
    args: JsonValue


class Cancellation:
    """The running ``kardia-guest/2`` export (0: none) and the last one its host cancelled."""

    def __init__(self) -> None:
        """Start with no export running."""
        self.running = 0
        self.cancelled = 0

    def is_cancelled(self) -> bool:
        """Report whether the running export was cancelled."""
        return self.running != 0 and self.cancelled == self.running


CANCELLATION = Cancellation()


class Host(ABC):
    """Synchronous host calls available while a guest export is running."""

    def cancelled(self) -> bool:
        """Report whether the host cancelled the running export, so a long call may stop.

        Only a process guest served under ``kardia-guest/2`` is told; elsewhere this is False.
        """
        return CANCELLATION.is_cancelled()

    @abstractmethod
    def log(self, level: Level, text: str) -> None:
        """Send a log message without writing to the protocol's stdout."""

    @abstractmethod
    def provide(self, key: str, value: JsonValue) -> None:
        """Provide a service whose method calls are routed to this guest instance."""

    @abstractmethod
    def view(self, key: str) -> JsonValue:
        """Read a JSON view allowed by the plugin's declared injection and context."""

    @abstractmethod
    def invoke(self, key: str, method: str, args: JsonValue) -> JsonValue:
        """Call another guest service synchronously through its actual host binding."""


class Guest(Generic[C], ABC):
    """Implement one plugin instance, with a typed configuration and static module metadata."""

    Config: TypeForm[C]

    @staticmethod
    @abstractmethod
    def meta() -> Meta:
        """Describe the module's exports and static dependencies."""

    @classmethod
    @abstractmethod
    def apply(cls, host: Host, config: C, imports: dict[str, JsonValue]) -> Self:
        """Start a fresh instance using decoded config and dependency JSON values."""

    def call(self, host: Host, request: Request) -> JsonValue:
        """Reject methods by default with the original guest SDK's diagnostic."""
        raise Error(f"{request.key} has no method {request.method}")

    def dispose(self, host: Host) -> None:
        """Release this guest instance before its transport is closed."""
        return None
