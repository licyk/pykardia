"""Native C ABI guest libraries loaded from a distinct copy for every module version."""

from __future__ import annotations

import ctypes
import itertools
import os
import shutil
import tempfile
from pathlib import Path

from pykardia.hmr.graph import ModuleCode, ModuleSource
from pykardia.hmr.guest import Code, Export, Host, Instance, module

Read = ctypes.CFUNCTYPE(None, ctypes.py_object, ctypes.c_void_p)
Write = ctypes.CFUNCTYPE(None, ctypes.py_object, ctypes.c_void_p, ctypes.c_size_t)
Log = ctypes.CFUNCTYPE(None, ctypes.py_object, ctypes.c_int32, ctypes.c_void_p, ctypes.c_size_t)
Provide = ctypes.CFUNCTYPE(
    ctypes.c_int32,
    ctypes.py_object,
    ctypes.c_void_p,
    ctypes.c_size_t,
    ctypes.c_void_p,
    ctypes.c_size_t,
)
View = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.py_object, ctypes.c_void_p, ctypes.c_size_t)
Invoke = ctypes.CFUNCTYPE(
    ctypes.c_int32,
    ctypes.py_object,
    ctypes.c_void_p,
    ctypes.c_size_t,
    ctypes.c_void_p,
    ctypes.c_size_t,
    ctypes.c_void_p,
    ctypes.c_size_t,
)


class HostApi(ctypes.Structure):
    """The original cordis_host layout; its opaque pointer carries a retained Python Host."""

    _fields_ = [
        ("host", ctypes.py_object),
        ("read", Read),
        ("write", Write),
        ("log", Log),
        ("provide", Provide),
        ("view", View),
        ("invoke", Invoke),
    ]


_LIBRARIES: list[ctypes.CDLL] = []
_COPIES = itertools.count()


class DylibSource(ModuleSource):
    """Load fresh native guest code while retaining every library mapping until process exit."""

    def load(self, id: str) -> ModuleCode:
        """Copy, load and read metadata for a new version of the given library."""
        return module(id, DylibCode(open_library(Path(id))))


class DylibCode(Code):
    """Required ABI function pointers of one permanently retained library version."""

    def __init__(self, library: ctypes.CDLL) -> None:
        """Configure the original signatures without translating the guest ABI."""
        self.library = library
        self.new = library.cordis_new
        self.new.argtypes, self.new.restype = [], ctypes.c_void_p
        self.meta = library.cordis_meta
        self.meta.argtypes, self.meta.restype = (
            [ctypes.c_void_p, ctypes.POINTER(HostApi)],
            ctypes.c_int32,
        )
        self.apply = library.cordis_apply
        self.apply.argtypes, self.apply.restype = (
            [ctypes.c_void_p, ctypes.POINTER(HostApi), ctypes.c_size_t],
            ctypes.c_int32,
        )
        self.call = library.cordis_call
        self.call.argtypes, self.call.restype = self.apply.argtypes, ctypes.c_int32
        self.dispose = library.cordis_dispose
        self.dispose.argtypes, self.dispose.restype = (
            [ctypes.c_void_p, ctypes.POINTER(HostApi)],
            None,
        )

    def instantiate(self, host: Host) -> DylibInstance:
        """Allocate a fresh guest-owned state pointer for one metadata or plugin instance."""
        return DylibInstance(self, host)


class DylibInstance(Instance):
    """A native guest state with callback references kept alive for every synchronous call."""

    def __init__(self, code: DylibCode, host: Host) -> None:
        """Allocate native state and retain the complete host callback table."""
        self.code = code
        self.host = host
        self.state = code.new()
        self.api = HostApi(
            host, Read(read), Write(write), Log(log), Provide(provide), View(view), Invoke(invoke)
        )

    def call(self, export: Export) -> int:
        """Dispatch the C export with pointer-sized lengths and the current callback table."""
        if export is Export.META:
            return self.code.meta(self.state, ctypes.byref(self.api))
        if export is Export.DISPOSE:
            self.code.dispose(self.state, ctypes.byref(self.api))
            return 0
        function = self.code.apply if export is Export.APPLY else self.code.call
        return function(self.state, ctypes.byref(self.api), len(self.host.message))

    def close(self) -> None:
        """Release Python references only; guest dispose owns state and mappings stay retained."""
        self.state = None


def open_library(path: Path) -> ctypes.CDLL:
    """Load a uniquely named copy and remove that copy where the OS permits it."""
    if not path.name:
        raise ValueError(f"{path} is not a file")
    directory = Path(tempfile.gettempdir()) / f"pykardia-dylib-{os.getpid()}"
    directory.mkdir(exist_ok=True)
    copy = directory / f"{next(_COPIES)}-{path.name}"
    shutil.copyfile(path, copy)
    library = ctypes.CDLL(str(copy))
    try:
        copy.unlink()
    except OSError:
        pass
    _LIBRARIES.append(library)
    return library


def read(host: Host, ptr: int | None) -> None:
    """Copy the current message into the guest's trusted destination buffer."""
    ctypes.memmove(ctypes.c_void_p(ptr), host.message, len(host.message))


def write(host: Host, ptr: int | None, length: int) -> None:
    """Copy a guest output buffer while its pointer remains valid."""
    host.write(bytes_at(ptr, length))


def log(host: Host, level: int, ptr: int | None, length: int) -> None:
    """Forward a guest log buffer through the shared bridge."""
    host.log(level, bytes_at(ptr, length))


def provide(host: Host, key: int | None, key_len: int, value: int | None, value_len: int) -> int:
    """Forward a guest provision with the exact pointer/length ABI."""
    return host.provide(bytes_at(key, key_len), bytes_at(value, value_len))


def view(host: Host, key: int | None, key_len: int) -> int:
    """Read the declared service named by a guest-owned UTF-8 buffer."""
    return host.view(bytes_at(key, key_len))


def invoke(
    host: Host,
    key: int | None,
    key_len: int,
    method: int | None,
    method_len: int,
    args: int | None,
    args_len: int,
) -> int:
    """Forward synchronous cross-guest invocation through the shared reentry guard."""
    return host.invoke(
        bytes_at(key, key_len), bytes_at(method, method_len), bytes_at(args, args_len)
    )


def bytes_at(ptr: int | None, length: int) -> bytes:
    """Read a trusted ABI slice, accepting a null pointer for an empty buffer."""
    return ctypes.string_at(ctypes.c_void_p(ptr), length) if length else b""
