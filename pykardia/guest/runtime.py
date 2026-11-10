# SPDX-License-Identifier: MIT OR Apache-2.0
"""Shared export dispatcher used by process, native-library and WASM guest adapters."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Generic, TypeVar

from pykardia.core.json import dumps
from pykardia.core.value import JsonValue
from pykardia.core.wire import config, from_slice
from pykardia.guest.api import Error, Guest, Host, Request

C = TypeVar("C")
T = TypeVar("T")


@dataclass
class Input:
    """The host's apply payload, with the same default config and imports as the source SDK."""

    config: JsonValue = field(default=None, metadata={"serde_default": True})
    imports: dict[str, JsonValue] = field(default_factory=dict)


class Runtime(Generic[C]):
    """One guest state slot behind the common exported entry points."""

    def __init__(self, guest: type[Guest[C]]) -> None:
        """Retain the implementation class before an instance has been applied."""
        self.guest = guest
        self.instance: Guest[C] | None = None

    def dispatch(self, export: str, host: Host, input: bytes) -> tuple[int, bytes | None]:
        """Run one export and translate Python exceptions into the protocol's status/output."""
        try:
            if export == "meta":
                return 0, dumps(guard(lambda: self.guest.meta().to_json()))
            if export == "apply":
                payload = from_slice(input, Input)
                decoded = config(payload.config, self.guest.Config)
                self.instance = guard(lambda: self.guest.apply(host, decoded, payload.imports))
                return 0, None
            if export == "call":
                request = from_slice(input, Request)
                applied = self.instance
                if applied is None:
                    raise Error("the guest has not been applied")
                return 0, dumps(guard(lambda: applied.call(host, request)))
            if export == "dispose":
                instance, self.instance = self.instance, None
                if instance is not None:
                    try:
                        guard(lambda: instance.dispose(host))
                    except Exception:
                        pass
                return 0, None
            return 1, None
        except Exception as error:
            return 1, str(error).encode("utf-8")


def guard(body: Callable[[], T]) -> T:
    """Contain guest exceptions like source panics while preserving explicit SDK errors."""
    try:
        return body()
    except Error:
        raise
    except BaseException as error:
        raise Error(f"the guest panicked: {error}") from error
