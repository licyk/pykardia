# SPDX-License-Identifier: MIT OR Apache-2.0
"""CFFI embedding callbacks for native guest libraries."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, cast

from pykardia.guest.abi import AbiHost
from pykardia.guest.api import Guest
from pykardia.guest.runtime import Runtime

if TYPE_CHECKING:
    from cffi import FFI


class HostApi(Protocol):
    """Typed view of the C host callback table received from CFFI."""

    host: object

    def read(self, host: object, buffer: object) -> None:
        """Copy the current message into a guest buffer."""
        ...

    def write(self, host: object, buffer: bytes, length: int) -> None:
        """Write guest output."""
        ...

    def log(self, host: object, level: int, buffer: bytes, length: int) -> None:
        """Forward a guest log."""
        ...

    def provide(self, host: object, key: bytes, kl: int, value: bytes, vl: int) -> int:
        """Provide a service and return a result length."""
        ...

    def view(self, host: object, key: bytes, kl: int) -> int:
        """View a service and return a result length."""
        ...

    def invoke(
        self, host: object, key: bytes, kl: int, method: bytes, ml: int, args: bytes, al: int
    ) -> int:
        """Invoke a service and return a result length."""
        ...


class NativeHost:
    """Keep CFFI allocations alive for the duration of each host callback."""

    def __init__(self, ffi: FFI, api: HostApi) -> None:
        """Retain the active FFI interface and borrowed host API."""
        self.ffi, self.api = ffi, api

    def read(self, length: int) -> bytes:
        """Allocate a guest-owned destination and copy its exact contents."""
        buffer = self.ffi.new("unsigned char[]", length)
        self.api.read(self.api.host, buffer)
        return bytes(self.ffi.buffer(buffer, length))

    def write(self, data: bytes) -> None:
        """Write a borrowed Python bytes buffer."""
        self.api.write(self.api.host, data, len(data))

    def log(self, level: int, data: bytes) -> None:
        """Log a borrowed UTF-8 buffer."""
        self.api.log(self.api.host, level, data, len(data))

    def provide(self, key: bytes, value: bytes) -> int:
        """Forward both buffers using the original C structure."""
        return self.api.provide(self.api.host, key, len(key), value, len(value))

    def view(self, key: bytes) -> int:
        """Forward the service name and retain the returned length."""
        return self.api.view(self.api.host, key, len(key))

    def invoke(self, key: bytes, method: bytes, args: bytes) -> int:
        """Forward service, method and JSON argument buffers."""
        return self.api.invoke(self.api.host, key, len(key), method, len(method), args, len(args))


def install(ffi: FFI, guest: type[Guest[object]]) -> None:
    """Register the C symbols, under both names, and retain state handles until disposal."""
    states: dict[int, object] = {}

    @ffi.def_extern()
    def cordis_new() -> object:
        """Allocate one independent runtime and retain its opaque state handle."""
        handle = ffi.new_handle(Runtime(guest))
        states[int(ffi.cast("size_t", handle))] = handle
        return handle

    def dispatch(state: object, api: HostApi, export: str, length: int = 0) -> int:
        """Read the input before any callback can replace the host message buffer."""
        runtime = cast(Runtime[object], ffi.from_handle(state))
        raw = NativeHost(ffi, api)
        status, output = runtime.dispatch(export, AbiHost(raw), raw.read(length) if length else b"")
        if output is not None:
            raw.write(output)
        return status

    @ffi.def_extern()
    def cordis_meta(state: object, api: HostApi) -> int:
        """Return metadata without starting an instance."""
        return dispatch(state, api, "meta")

    @ffi.def_extern()
    def cordis_apply(state: object, api: HostApi, length: int) -> int:
        """Apply typed configuration to this state slot."""
        return dispatch(state, api, "apply", length)

    @ffi.def_extern()
    def cordis_call(state: object, api: HostApi, length: int) -> int:
        """Call the current instance and translate errors into ABI output."""
        return dispatch(state, api, "call", length)

    @ffi.def_extern()
    def cordis_dispose(state: object, api: HostApi) -> None:
        """Dispose the instance and release the opaque handle exactly once."""
        try:
            dispatch(state, api, "dispose")
        finally:
            states.pop(int(ffi.cast("size_t", state)))

    # the same exports under the kardia_* names; hosts that know only cordis_* use those

    @ffi.def_extern()
    def kardia_new() -> object:
        """Allocate a state, as cordis_new does."""
        return cordis_new()

    @ffi.def_extern()
    def kardia_meta(state: object, api: HostApi) -> int:
        """Return metadata, as cordis_meta does."""
        return cordis_meta(state, api)

    @ffi.def_extern()
    def kardia_apply(state: object, api: HostApi, length: int) -> int:
        """Apply, as cordis_apply does."""
        return cordis_apply(state, api, length)

    @ffi.def_extern()
    def kardia_call(state: object, api: HostApi, length: int) -> int:
        """Call, as cordis_call does."""
        return cordis_call(state, api, length)

    @ffi.def_extern()
    def kardia_dispose(state: object, api: HostApi) -> None:
        """Dispose, as cordis_dispose does."""
        cordis_dispose(state, api)
