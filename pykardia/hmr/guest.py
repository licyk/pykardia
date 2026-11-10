"""Guest transports, host callbacks and the per-fiber bridge plugin."""

from __future__ import annotations

import asyncio
import threading
import time
import weakref
from abc import ABC, abstractmethod
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from pykardia.core.cleanup import Disposer
from pykardia.core.context import Context
from pykardia.core.json import dumps, loads
from pykardia.core.logger import Level, Logger
from pykardia.core.plugin import PluginDef
from pykardia.core.value import JsonValue
from pykardia.core.wire import from_slice
from pykardia.hmr.exit import ExitCause, GuestExited, GuestHandle, GuestStarted
from pykardia.hmr.graph import ExportKind, Exports, Imports, ModuleCode
from pykardia.hmr.transaction import normalize


class Export(Enum):
    """The four operations of the guest ABI."""

    META = "meta"
    APPLY = "apply"
    CALL = "call"
    DISPOSE = "dispose"


class CallControl:
    """A call's deadline, at which a transport stops its guest, and its cancellation."""

    def __init__(self, deadline: float | None = None) -> None:
        """Bound a call by a ``time.monotonic`` deadline; ``None`` leaves the transport default."""
        self._deadline = deadline
        self._cancelled = False
        self._wakers: list[Callable[[], None]] = []
        self._lock = threading.Lock()

    @classmethod
    def timeout(cls, timeout: float | None) -> CallControl:
        """Bound a call by a timeout in seconds from now."""
        return cls(None if timeout is None else time.monotonic() + timeout)

    def deadline(self) -> float | None:
        """Return the monotonic deadline, if the caller set one."""
        return self._deadline

    def is_cancelled(self) -> bool:
        """Report whether the caller no longer wants the result."""
        return self._cancelled

    def cancel(self) -> None:
        """Cancel the call and run its wakers once."""
        with self._lock:
            if self._cancelled:
                return
            self._cancelled = True
            wakers, self._wakers = self._wakers, []
        for wake in wakers:
            wake()

    def on_cancel(self, wake: Callable[[], None]) -> None:
        """Run ``wake`` when the call is cancelled, at once if it already is."""
        with self._lock:
            if not self._cancelled:
                self._wakers.append(wake)
                return
        wake()


class GuestCode(ABC):
    """Guest code of one module version: how a guest runs, instantiated once per fiber."""

    @abstractmethod
    def instantiate(self, host: GuestHost) -> GuestInstance:
        """Start an instance whose host calls act on ``host``; it may block, off the loop."""


class GuestInstance(ABC):
    """A running guest owned by one fiber, called from one thread at a time."""

    @abstractmethod
    def call(self, export: Export, input: bytes, control: CallControl) -> bytes:
        """Call an export with UTF-8 JSON input and return its output, raising its error.

        A transport that can stop its guest stops it at the control's deadline (or its own
        default), failing this and every later call, and drops a cancelled call's result.
        """

    @abstractmethod
    def close(self) -> None:
        """Release the guest after its last call, whether or not that call succeeded."""

    def handle(self) -> GuestHandle | None:
        """Return the process running the guest, for a transport that has one."""
        return None


class AbiInstance(GuestInstance):
    """An in-process guest speaking the integer status ABI over its host's buffers."""

    host: GuestHost

    @abstractmethod
    def export(self, export: Export) -> int:
        """Call the export on ``host.message`` and return the guest's status."""

    def call(self, export: Export, input: bytes, control: CallControl) -> bytes:
        """Run the export; an in-process guest cannot be stopped at a deadline."""
        self.host.begin(input)
        return self.host.finish(self.export(export))


class Slot:
    """The instance a fiber owns, empty once disposed, serialized across threads."""

    def __init__(self) -> None:
        """Start an empty slot before instantiating its guest."""
        self.instance: GuestInstance | None = None
        self.lock = threading.Lock()
        self.disposed = False
        self.handle: GuestHandle | None = None

    def is_available(self) -> bool:
        """Report whether the instance can still answer."""
        if self.handle is not None and self.handle.exit() is not None:
            return False
        return not self.disposed


class Entered(threading.local):
    """Per-thread instance calls used to reject direct and indirect guest reentry."""

    def __init__(self) -> None:
        """Initialize the active call stack for the current thread."""
        self.slots: list[Slot] = []


_ENTERED = Entered()


class GuestHost:
    """The host side of one guest instance: its fiber and the ABI's host calls."""

    def __init__(self, ctx: Context | None, slot: Slot | None) -> None:
        """Retain context and a weak service-call route without creating an ownership cycle."""
        self._ctx = ctx
        self._slot = weakref.ref(slot) if slot is not None else None
        self.message = b""
        self.output = b""

    def context(self) -> Context | None:
        """Return the fiber's context, or ``None`` while module metadata is read."""
        return self._ctx

    def logger(self) -> Logger | None:
        """Return the plugin's logger, without one while module metadata is read."""
        return None if self._ctx is None else self._ctx.logger()

    def log(self, level: Level, text: str) -> None:
        """Log as the plugin."""
        if self._ctx is not None:
            self._ctx.logger().log(level, text)

    def provide(self, key: str, value: JsonValue) -> None:
        """Provide ``key`` as a GuestService whose view is ``value``."""
        service = GuestService(key, value, self._slot)
        self.context_or_error().provide_serialized_as(key, service, GuestService.value)

    def view(self, key: str) -> JsonValue:
        """Read a declared service's JSON view."""
        return self.context_or_error().view(key)

    def invoke(self, key: str, method: str, args: JsonValue) -> JsonValue:
        """Call another guest service through the checked caller context."""
        return self.context_or_error().get_as(key, GuestService).arc().call(method, args)

    def context_or_error(self) -> Context:
        """Reject service callbacks while the separate metadata instance is running."""
        if self._ctx is None:
            raise ValueError("host calls are unavailable while reading module metadata")
        return self._ctx

    # The in-process backends' side: bytes in, the status convention out.

    def begin(self, input: bytes) -> None:
        """Set an export input and clear the previous export output."""
        self.message = input
        self.output = b""

    def finish(self, status: int) -> bytes:
        """Return the output of a successful export or raise the guest's error."""
        output, self.output = self.output, b""
        if status == 0:
            return output
        if output:
            raise ValueError(output.decode("utf-8", errors="replace"))
        raise ValueError(f"guest failed with status {status}")

    def write(self, data: bytes) -> None:
        """Replace the current export's output without changing its input/reply message."""
        self.output = data

    def log_raw(self, level: int, text: bytes) -> None:
        """Log an ABI level, defaulting unknown levels to Info."""
        self.log(abi_level(level), text.decode("utf-8", errors="replace"))

    def provide_raw(self, key: bytes, value: bytes) -> int:
        """Provide from ABI buffers and reply with empty success or an error."""

        def perform() -> bytes:
            """Decode the external provision once and attach it to the plugin scope."""
            self.provide(utf8(key), loads(value))
            return b""

        return self.reply(perform)

    def view_raw(self, key: bytes) -> int:
        """Read a view into the reply buffer."""
        return self.reply(lambda: dumps(self.view(utf8(key))))

    def invoke_raw(self, key: bytes, method: bytes, args: bytes) -> int:
        """Invoke a guest service from ABI buffers into the reply buffer."""
        return self.reply(lambda: dumps(self.invoke(utf8(key), utf8(method), loads(args))))

    def reply(self, operation: Callable[[], bytes]) -> int:
        """Store callback output and encode its byte length, using -1-length for errors."""
        try:
            self.message = operation()
            return len(self.message)
        except Exception as error:
            self.message = str(error).encode("utf-8")
            return -1 - len(self.message)


class GuestService:
    """A guest-provided JSON view and a route to its owning instance."""

    def __init__(
        self, key: str, value: JsonValue, slot: weakref.ReferenceType[Slot] | None
    ) -> None:
        """Retain the public view without extending the guest instance's lifetime."""
        self._key = key
        self._value = deepcopy(value)
        self._slot = slot
        owner = slot() if slot is not None else None
        # kept to tell why the guest is gone once its instance is
        self._handle = owner.handle if owner is not None else None

    def value(self) -> JsonValue:
        """Return a copy of the original provided view, independently of later guest state."""
        return deepcopy(self._value)

    def call(self, method: str, args: JsonValue) -> JsonValue:
        """Call the guest, blocking until it answers or its transport's default timeout."""
        return self.call_with(method, args, CallControl())

    def call_timeout(self, method: str, args: JsonValue, timeout: float) -> JsonValue:
        """Call the guest, failing after ``timeout`` seconds; a process guest is then killed."""
        return self.call_with(method, args, CallControl.timeout(timeout))

    async def call_async(
        self, method: str, args: JsonValue, timeout: float | None = None
    ) -> JsonValue:
        """Call the guest off the event loop; cancelling the awaiting task cancels the call."""
        control = CallControl.timeout(timeout)
        call = asyncio.ensure_future(asyncio.to_thread(self.call_with, method, args, control))
        try:
            return await asyncio.shield(call)
        except asyncio.CancelledError:
            control.cancel()
            # nobody awaits the abandoned call's error
            call.add_done_callback(lambda done: done.cancelled() or done.exception())
            raise

    def is_available(self) -> bool:
        """Report whether the instance is neither disposed nor exited."""
        slot = self._slot() if self._slot is not None else None
        return slot is not None and slot.is_available()

    def handle(self) -> GuestHandle | None:
        """Return the process running the guest, for a transport that has one."""
        return self._handle

    def call_with(self, method: str, args: JsonValue, control: CallControl) -> JsonValue:
        """Invoke the owning instance and decode its result."""
        slot = self._slot() if self._slot is not None else None
        if slot is None:
            raise gone(self._handle)
        input = dumps({"key": self._key, "method": method, "args": args})
        output = run(slot, Export.CALL, input, control)
        try:
            return loads(output)
        except ValueError as error:
            raise ValueError(f"invalid result of {self._key}.{method}: {error}") from error


@dataclass
class GuestMeta:
    """A guest plugin's metadata: its name and the services it reads and provides."""

    name: str | None = None
    inject: list[str] = field(default_factory=list)
    provide: list[str] = field(default_factory=list)

    def matches(self, other: GuestMeta) -> bool:
        """Compare names and service sets, in any order."""
        return (
            self.name == other.name
            and set(self.inject) == set(other.inject)
            and set(self.provide) == set(other.provide)
        )

    def to_json(self) -> JsonValue:
        """Render the metadata as the ABI writes it."""
        return {"name": self.name, "inject": list(self.inject), "provide": list(self.provide)}


@dataclass
class ModuleMeta:
    """The host's decoded metadata, where a null value is the same as no value export."""

    deps: list[str] = field(default_factory=list)
    plugin: GuestMeta | None = None
    value: JsonValue = None


def guest_module(id: str, code: GuestCode) -> ModuleCode:
    """Read and dispose a separate metadata instance before creating module evaluation code."""
    instance = code.instantiate(GuestHost(None, None))
    failure: Exception | None = None
    output = b""
    try:
        try:
            output = instance.call(Export.META, b"", CallControl())
        except Exception as error:
            failure = error
        instance.call(Export.DISPOSE, b"", CallControl())
    finally:
        instance.close()
    if failure is not None:
        raise failure
    try:
        meta = from_slice(output, ModuleMeta)
    except Exception as error:
        raise ValueError(f"invalid metadata of module {id}: {error}") from error
    deps = [
        "" if (path := normalize(Path(id).parent / dep)) == Path() else str(path)
        for dep in meta.deps
    ]
    plugin = meta.plugin

    def evaluate(imports: Imports) -> Exports:
        """Prefer plugin exports and pass only static dependencies that export JSON values."""
        if plugin is not None:
            values: dict[str, JsonValue] = {}
            for dep in deps:
                exported = imports.get(dep)
                if exported is not None and exported.kind is ExportKind.VALUE:
                    values[dep] = exported.json_value
            return Exports.plugin(bridge(code, plugin, values, check=False))
        return Exports.value(meta.value) if meta.value is not None else Exports.none()

    return ModuleCode.new(deps, evaluate)


def guest_plugin(code: GuestCode, meta: GuestMeta) -> PluginDef:
    """Build a plugin with caller-given metadata; each instance must declare the same."""
    return bridge(code, meta, {}, check=True)


def bridge(
    code: GuestCode, meta: GuestMeta, imports: dict[str, JsonValue], *, check: bool
) -> PluginDef:
    """Build a plugin that owns one instance per fiber, calling it off the event loop."""

    async def apply(ctx: Context, config: JsonValue) -> None:
        """Instantiate, register disposal, check metadata, apply and watch the process."""
        slot = Slot()
        instance = await asyncio.to_thread(code.instantiate, GuestHost(ctx, slot))
        slot.instance, slot.handle = instance, instance.handle()
        logger = ctx.logger()

        async def dispose() -> None:
            """Dispose the instance off the loop, reporting its errors."""
            try:
                await asyncio.to_thread(run, slot, Export.DISPOSE, b"", CallControl())
            except Exception as error:
                logger.log_error(error)

        ctx.defer("guest", Disposer.future(dispose))
        handle = slot.handle
        if handle is not None:
            ctx.emit(GuestStarted(ctx.fiber(), handle))
        if check:
            await asyncio.to_thread(check_meta, slot, meta)
        input = dumps({"config": config, "imports": imports})
        await asyncio.to_thread(run, slot, Export.APPLY, input, CallControl())
        if handle is not None:
            watch_exit(ctx, handle)

    definition = PluginDef.new(apply).inject(meta.inject).provides(meta.provide)
    return definition.name(meta.name) if meta.name is not None else definition


def watch_exit(ctx: Context, handle: GuestHandle) -> None:
    """Fail the fiber when the guest's process ends before the fiber disposes it."""

    async def watch() -> None:
        """Wait for the exit report, then report it and fail the fiber."""
        exit = await handle.exited()
        if exit.cause is ExitCause.DISPOSED:
            return
        fiber = ctx.fiber()
        ctx.emit(GuestExited(fiber, exit))
        fiber.fail(ValueError(str(exit)))

    task = asyncio.get_running_loop().create_task(watch())

    def stop() -> None:
        """Stop watching once the fiber disposes the guest."""
        task.cancel()

    ctx.on_dispose(stop)


def check_meta(slot: Slot, expected: GuestMeta) -> None:
    """Compare a started instance's own metadata with the caller's."""
    output = run(slot, Export.META, b"", CallControl())
    try:
        meta = from_slice(output, ModuleMeta)
    except Exception as error:
        raise ValueError(f"invalid metadata of the guest: {error}") from error
    actual = meta.plugin
    if actual is None or not actual.matches(expected):
        described = dumps(None if actual is None else actual.to_json()).decode("utf-8")
        defined = dumps(expected.to_json()).decode("utf-8")
        raise ValueError(f"the guest declares {described}, but its plugin is defined as {defined}")


def run(slot: Slot, export: Export, input: bytes, control: CallControl) -> bytes:
    """Serialize guest calls, reject same-thread reentry and clear disposed slots."""
    if slot in _ENTERED.slots:
        raise ValueError("re-entrant call into a guest instance")
    _ENTERED.slots.append(slot)
    try:
        deadline = control.deadline()
        timeout = -1 if deadline is None else max(0.0, deadline - time.monotonic())
        # waiting for another call to finish: this one fails, the guest is not stopped
        if not slot.lock.acquire(timeout=timeout):
            raise timed_out()
        try:
            if control.is_cancelled():
                raise ValueError("the guest call was cancelled")
            instance = slot.instance
            if instance is None:
                raise gone(slot.handle)
            try:
                return instance.call(export, input, control)
            finally:
                if export is Export.DISPOSE:
                    slot.instance = None
                    slot.disposed = True
                    instance.close()
        finally:
            slot.lock.release()
    finally:
        _ENTERED.slots.pop()


def gone(handle: GuestHandle | None) -> ValueError:
    """Explain an empty slot: its process exited on its own, or its fiber disposed it."""
    exit = handle.exit() if handle is not None else None
    if exit is not None and exit.cause is not ExitCause.DISPOSED:
        return exited()
    return ValueError("the guest instance is disposed")


def exited() -> ValueError:
    """Return the error of a call to a guest whose process has ended."""
    return ValueError("the guest process exited")


def timed_out() -> ValueError:
    """Return the error of a call past its deadline."""
    return ValueError("the guest did not answer in time")


def abi_level(level: int) -> Level:
    """Map an ABI log level, defaulting unknown levels to Info."""
    return Level(level) if level in (0, 1, 2, 3) else Level.Info


def utf8(data: bytes) -> str:
    """Decode ABI names with the source's byte-index UTF-8 diagnostic."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        if error.reason == "unexpected end of data":
            message = f"incomplete utf-8 byte sequence from index {error.start}"
        else:
            length = error.end - error.start
            message = f"invalid utf-8 sequence of {length} bytes from index {error.start}"
        raise ValueError(message) from error
