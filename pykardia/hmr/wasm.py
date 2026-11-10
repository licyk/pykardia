"""Wasmtime guest modules compiled per version with independent stores per instance."""

from __future__ import annotations

import wasmtime

from pykardia.hmr.graph import ModuleCode, ModuleSource
from pykardia.hmr.guest import AbiInstance, Export, GuestCode, GuestHost, guest_module


class WasmSource(ModuleSource):
    """Load WASM binaries or WAT text importing the host from the kardia or cordis module.

    Exports are found as ``kardia_*``, else ``cordis_*``.
    """

    def __init__(self) -> None:
        """Create the shared engine used to compile each loaded module version."""
        self.engine = wasmtime.Engine()

    def load(self, id: str) -> ModuleCode:
        """Compile current file contents and read metadata from an independent instance."""
        compiled = wasmtime.Module.from_file(self.engine, id)
        return guest_module(id, WasmCode(self.engine, compiled))


class WasmCode(GuestCode):
    """One compiled module version capable of creating isolated guest stores."""

    def __init__(self, engine: wasmtime.Engine, compiled: wasmtime.Module) -> None:
        """Retain the engine and compiled code while old and new versions coexist."""
        self.engine = engine
        self.compiled = compiled

    def instantiate(self, host: GuestHost) -> WasmInstance:
        """Create fresh memory, guest globals and callbacks for one instance."""
        return WasmInstance(self, host)


class WasmInstance(AbiInstance):
    """A store whose exported functions and host callbacks use the original integer ABI."""

    def __init__(self, code: WasmCode, host: GuestHost) -> None:
        """Install host functions and instantiate fresh guest state."""
        self.host = host
        self.store = wasmtime.Store(code.engine)
        self.linker = wasmtime.Linker(code.engine)
        define(self.linker, host)
        try:
            self.instance = self.linker.instantiate(self.store, code.compiled)
        except Exception:
            self.close()
            raise

    def export(self, export: Export) -> int:
        """Call a typed guest export, treating an absent dispose function as success."""
        exports = self.instance.exports(self.store)
        name = f"kardia_{export.value}"
        function = exports.get(name)
        if not isinstance(function, wasmtime.Func):
            # a guest without the kardia_* name for this export
            name = f"cordis_{export.value}"
            function = exports.get(name)
        if not isinstance(function, wasmtime.Func):
            if export is Export.DISPOSE:
                return 0
            raise ValueError(f"failed to find function export `{name}`")
        signature = function.type(self.store)
        params = [wasmtime.ValType.i32()] if export in (Export.APPLY, Export.CALL) else []
        results = [] if export is Export.DISPOSE else [wasmtime.ValType.i32()]
        for field, expected, actual in (
            ("parameters", params, signature.params),
            ("results", results, signature.results),
        ):
            if expected == actual:
                continue
            if len(expected) != len(actual):
                detail = f"expected {len(expected)} types, found {len(actual)}"
            else:
                want, got = next(
                    (want, got) for want, got in zip(expected, actual, strict=True) if want != got
                )
                detail = f"type mismatch: expected {want}, found {got}"
            prefix = (
                ""
                if export is Export.DISPOSE
                else (f"failed to convert function `{name}` to given type: ")
            )
            raise ValueError(f"{prefix}type mismatch with {field}: {detail}")
        try:
            if params:
                return function(self.store, len(self.host.message))
            result = function(self.store)
            return 0 if export is Export.DISPOSE else result
        except wasmtime.Trap as error:
            raise ValueError(str(error).replace("\n\nCaused by:\n    ", ": ")) from error

    def close(self) -> None:
        """Release this instance's store memory and callback table after its last call."""
        self.store.close()
        self.linker.close()


def define(linker: wasmtime.Linker, host: GuestHost) -> None:
    """Bind the six guest imports under both modules, kardia and cordis, without WASI."""

    def read(caller: wasmtime.Caller, ptr: int) -> None:
        """Copy the current host message into a checked guest memory range."""
        mem = memory(caller)
        start = ptr & 0xFFFFFFFF
        if start + len(host.message) > mem.data_len(caller):
            raise wasmtime.Trap("guest memory access out of bounds")
        if host.message:
            mem.write(caller, host.message, start)

    def write(caller: wasmtime.Caller, ptr: int, length: int) -> None:
        """Copy the checked guest output buffer into the host result."""
        host.write(bytes_at(caller, ptr, length))

    def log(caller: wasmtime.Caller, level: int, ptr: int, length: int) -> None:
        """Forward a checked UTF-8 guest log buffer through the bridge."""
        host.log_raw(level, bytes_at(caller, ptr, length))

    def provide(caller: wasmtime.Caller, key: int, key_len: int, value: int, value_len: int) -> int:
        """Decode pointer slices before providing the guest's service."""
        return host.provide_raw(bytes_at(caller, key, key_len), bytes_at(caller, value, value_len))

    def view(caller: wasmtime.Caller, key: int, key_len: int) -> int:
        """Read a checked guest key and return the host reply byte length."""
        return host.view_raw(bytes_at(caller, key, key_len))

    def invoke(
        caller: wasmtime.Caller,
        key: int,
        key_len: int,
        method: int,
        method_len: int,
        args: int,
        args_len: int,
    ) -> int:
        """Resolve all ABI slices before entering another guest instance."""
        return host.invoke_raw(
            bytes_at(caller, key, key_len),
            bytes_at(caller, method, method_len),
            bytes_at(caller, args, args_len),
        )

    i32 = wasmtime.ValType.i32()
    for module in ("kardia", "cordis"):
        linker.define_func(module, "read", wasmtime.FuncType([i32], []), read, access_caller=True)
        linker.define_func(
            module, "write", wasmtime.FuncType([i32, i32], []), write, access_caller=True
        )
        linker.define_func(
            module, "log", wasmtime.FuncType([i32, i32, i32], []), log, access_caller=True
        )
        linker.define_func(
            module, "provide", wasmtime.FuncType([i32] * 4, [i32]), provide, access_caller=True
        )
        linker.define_func(
            module, "view", wasmtime.FuncType([i32, i32], [i32]), view, access_caller=True
        )
        linker.define_func(
            module, "invoke", wasmtime.FuncType([i32] * 6, [i32]), invoke, access_caller=True
        )


def memory(caller: wasmtime.Caller) -> wasmtime.Memory:
    """Require the guest's exported linear memory for an ABI buffer operation."""
    exported = caller.get("memory")
    if not isinstance(exported, wasmtime.Memory):
        raise wasmtime.Trap("the guest exports no memory")
    return exported


def bytes_at(caller: wasmtime.Caller, ptr: int, length: int) -> bytes:
    """Read an unsigned pointer/length slice, rejecting bounds before Wasmtime's clamping API."""
    mem = memory(caller)
    start, length = ptr & 0xFFFFFFFF, length & 0xFFFFFFFF
    if start + length > mem.data_len(caller):
        raise wasmtime.Trap("guest memory access out of bounds")
    return bytes(mem.read(caller, start, start + length))
