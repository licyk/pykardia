"""Shared guest ABI, host callbacks and per-fiber bridge plugin instances."""

from __future__ import annotations

import threading
import weakref
from abc import ABC, abstractmethod
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from pykardia.core.context import Context
from pykardia.core.json import dumps, loads
from pykardia.core.logger import Level
from pykardia.core.plugin import PluginDef
from pykardia.core.value import JsonValue
from pykardia.core.wire import from_slice
from pykardia.hmr.graph import ExportKind, Exports, Imports, ModuleCode
from pykardia.hmr.transaction import normalize


class Export(Enum):
    """The four synchronous operations supported by a guest instance."""

    META = "meta"
    APPLY = "apply"
    CALL = "call"
    DISPOSE = "dispose"


class Code(ABC):
    """Compiled or otherwise loadable code for one module version."""

    @abstractmethod
    def instantiate(self, host: Host) -> Instance:
        """Create a fresh guest state retaining the supplied host callbacks."""


class Instance(ABC):
    """One synchronous guest state and the host message buffers of its current call."""

    host: Host

    @abstractmethod
    def call(self, export: Export) -> int:
        """Call the requested export using host.message and return its status."""

    @abstractmethod
    def close(self) -> None:
        """Release backend resources after the last export, independently of its success."""


class Slot:
    """A fiber-owned instance slot, empty after disposal and serialized across threads."""

    def __init__(self) -> None:
        """Start an empty slot before instantiating its guest."""
        self.instance: Instance | None = None
        self.lock = threading.RLock()


class Entered(threading.local):
    """Per-thread instance calls used to reject direct and indirect guest reentry."""

    def __init__(self) -> None:
        """Initialize the active call stack for the current thread."""
        self.slots: list[Slot] = []


_ENTERED = Entered()


class Host:
    """Host-side buffers and callbacks for one metadata or plugin instance."""

    def __init__(self, ctx: Context | None, slot: Slot | None) -> None:
        """Retain context and a weak service-call route without creating an ownership cycle."""
        self._ctx = ctx
        self._slot = weakref.ref(slot) if slot is not None else None
        self.message = b""
        self.output = b""

    def begin(self, input: bytes) -> None:
        """Set an export input and clear the previous export output."""
        self.message = input
        self.output = b""

    def write(self, data: bytes) -> None:
        """Replace the current export's output without changing its input/reply message."""
        self.output = data

    def log(self, level: int, text: bytes) -> None:
        """Log as the plugin, defaulting unknown levels to Info and ignoring metadata logs."""
        if self._ctx is not None:
            self._ctx.logger().log(
                Level(level) if level in (0, 1, 2, 3) else Level.Info,
                text.decode("utf-8", errors="replace"),
            )

    def provide(self, key: bytes, value: bytes) -> int:
        """Provide a serializable guest service and reply with empty success or an error."""

        def perform() -> bytes:
            """Decode the external provision once and attach it to the actual plugin scope."""
            name = utf8(key)
            service = GuestService(name, loads(value), self._slot)
            self.context().provide_serialized_as(name, service, lambda service: service.value())
            return b""

        return self.reply(perform)

    def view(self, key: bytes) -> int:
        """Read a declared service's JSON view and write the result into the reply buffer."""
        return self.reply(lambda: dumps(self.context().view(utf8(key))))

    def invoke(self, key: bytes, method: bytes, args: bytes) -> int:
        """Call another guest service through the actual checked caller context."""

        def perform() -> bytes:
            """Resolve a typed guest service and invoke its synchronous method."""
            service = self.context().get_as(utf8(key), GuestService).arc()
            return dumps(service.call(utf8(method), loads(args)))

        return self.reply(perform)

    def context(self) -> Context:
        """Reject service callbacks while the separate metadata instance is running."""
        if self._ctx is None:
            raise ValueError("host calls are unavailable while reading module metadata")
        return self._ctx

    def reply(self, operation: Callable[[], bytes]) -> int:
        """Store callback output and encode its byte length, using -1-length for errors."""
        try:
            self.message = operation()
            return len(self.message)
        except Exception as error:
            self.message = str(error).encode("utf-8")
            return -1 - len(self.message)


class GuestService:
    """A guest-provided JSON view and synchronous route to its owning instance."""

    def __init__(
        self, key: str, value: JsonValue, slot: weakref.ReferenceType[Slot] | None
    ) -> None:
        """Retain the public view without extending the guest instance's lifetime."""
        self._key = key
        self._value = deepcopy(value)
        self._slot = slot

    def value(self) -> JsonValue:
        """Return a copy of the original provided view, independently of later guest state."""
        return deepcopy(self._value)

    def call(self, method: str, args: JsonValue) -> JsonValue:
        """Invoke the owning instance and decode its result or report that it was disposed."""
        slot = self._slot() if self._slot is not None else None
        if slot is None:
            raise ValueError("the guest instance is disposed")
        output = run(slot, Export.CALL, dumps({"key": self._key, "method": method, "args": args}))
        try:
            return loads(output)
        except ValueError as error:
            raise ValueError(f"invalid result of {self._key}.{method}: {error}") from error


@dataclass
class PluginMeta:
    """Guest-declared plugin metadata validated once when reading the module."""

    name: str | None = None
    inject: list[str] = field(default_factory=list)
    provide: list[str] = field(default_factory=list)


@dataclass
class Meta:
    """The host's decoded metadata, where a null value is the same as no value export."""

    deps: list[str] = field(default_factory=list)
    plugin: PluginMeta | None = None
    value: JsonValue = None


def module(id: str, code: Code) -> ModuleCode:
    """Read and dispose a separate metadata instance before creating module evaluation code."""
    instance = code.instantiate(Host(None, None))
    failure: Exception | None = None
    output = b""
    try:
        try:
            output = invoke(instance, Export.META, b"")
        except Exception as error:
            failure = error
        invoke(instance, Export.DISPOSE, b"")
    finally:
        instance.close()
    if failure is not None:
        raise failure
    try:
        meta = from_slice(output, Meta)
    except Exception as error:
        raise ValueError(f"invalid metadata of module {id}: {error}") from error
    deps = [
        "" if (path := normalize(Path(id).parent / dep)) == Path() else str(path)
        for dep in meta.deps
    ]

    def evaluate(imports: Imports) -> Exports:
        """Prefer plugin exports and pass only static dependencies that export JSON values."""
        if meta.plugin is not None:
            values: dict[str, JsonValue] = {}
            for dep in deps:
                exported = imports.get(dep)
                if exported is not None and exported.kind is ExportKind.VALUE:
                    values[dep] = exported.json_value
            return Exports.plugin(bridge(code, meta.plugin, values))
        return Exports.value(meta.value) if meta.value is not None else Exports.none()

    return ModuleCode.new(deps, evaluate)


def bridge(code: Code, meta: PluginMeta, imports: dict[str, JsonValue]) -> PluginDef:
    """Build a plugin that owns exactly one instance for each application episode."""

    async def apply(ctx: Context, config: JsonValue) -> None:
        """Instantiate and register disposal before calling guest application."""
        slot = Slot()
        slot.instance = code.instantiate(Host(ctx, slot))
        logger = ctx.logger()

        def dispose() -> None:
            """Report guest disposal errors while releasing backend resources regardless."""
            try:
                run(slot, Export.DISPOSE, b"")
            except Exception as error:
                logger.log_error(error)

        ctx.on_dispose(dispose)
        run(slot, Export.APPLY, dumps({"config": config, "imports": imports}))

    definition = PluginDef.new(apply).inject(meta.inject).provides(meta.provide)
    return definition.name(meta.name) if meta.name is not None else definition


def run(slot: Slot, export: Export, input: bytes) -> bytes:
    """Serialize guest calls, reject same-thread reentry and clear disposed slots."""
    if slot in _ENTERED.slots:
        raise ValueError("re-entrant call into a guest instance")
    _ENTERED.slots.append(slot)
    try:
        with slot.lock:
            instance = slot.instance
            if instance is None:
                raise ValueError("the guest instance is disposed")
            try:
                return invoke(instance, export, input)
            finally:
                if export is Export.DISPOSE:
                    slot.instance = None
                    instance.close()
    finally:
        _ENTERED.slots.pop()


def invoke(instance: Instance, export: Export, input: bytes) -> bytes:
    """Reset message/output, call one export and decode the documented status convention."""
    instance.host.begin(input)
    status = instance.call(export)
    output, instance.host.output = instance.host.output, b""
    if status == 0:
        return output
    if output:
        raise ValueError(output.decode("utf-8", errors="replace"))
    raise ValueError(f"guest failed with status {status}")


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
