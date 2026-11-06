"""Plugin definitions, ordered dependency declarations and stable registry identity."""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Iterable, Iterator
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Generic, Literal, TypeVar, cast

from typing_extensions import TypeForm

from pykardia.core.config import decode, decode_json
from pykardia.core.key import Key
from pykardia.core.value import JsonValue, json_equal

if TYPE_CHECKING:
    from pykardia.core.context import Context
    from pykardia.core.service import Service

C = TypeVar("C")
Apply = Callable[["Context", object], Awaitable[None]]
Decoder = Callable[[JsonValue], object]


class Inject:
    """Ordered dependency keys, each with optional interception metadata."""

    def __init__(self, keys: Iterable[str] = ()) -> None:
        """Create an ordered declaration, preserving the first position of duplicate keys."""
        self._entries: dict[Key, tuple[bool, JsonValue]] = {Key(key): (False, None) for key in keys}

    @classmethod
    def new(cls) -> Inject:
        """Create an empty declaration."""
        return cls()

    @classmethod
    def of(cls, *services: type[Service]) -> Inject:
        """Declare a tuple of service types through their coeffect names."""
        return cls(service.NAME for service in services)

    def with_(self, key: str) -> Inject:
        """Return a declaration with this key present and its metadata cleared."""
        result = self.copy()
        result._entries[Key(key)] = (False, None)
        return result

    def with_meta(self, key: str, meta: JsonValue) -> Inject:
        """Return a declaration with a metadata snapshot, including explicit JSON null."""
        result = self.copy()
        result._entries[Key(key)] = (True, deepcopy(meta))
        return result

    def copy(self) -> Inject:
        """Clone declaration data without sharing mutable JSON metadata."""
        result = Inject()
        result._entries = deepcopy(self._entries)
        return result

    def keys(self) -> Iterator[Key]:
        """Iterate declared service names in declaration order."""
        return iter(self._entries)

    def contains(self, key: str) -> bool:
        """Test whether this dependency is declared."""
        return Key(key) in self._entries

    def meta(self, key: str) -> JsonValue:
        """Return a metadata snapshot; use has_meta to distinguish absence from JSON null."""
        return deepcopy(self._entries.get(Key(key), (False, None))[1])

    def has_meta(self, key: str) -> bool:
        """Distinguish Rust's None from Some(Value::Null) at the Python boundary."""
        return self._entries.get(Key(key), (False, None))[0]

    def len(self) -> int:
        """Count declared keys."""
        return len(self._entries)

    def is_empty(self) -> bool:
        """Return whether the declaration has no dependencies."""
        return not self._entries

    def __eq__(self, other: object) -> bool:
        """Compare keys and metadata by JSON type, independent of map insertion order."""
        if not isinstance(other, Inject) or self._entries.keys() != other._entries.keys():
            return False
        return all(
            present == other._entries[key][0] and json_equal(meta, other._entries[key][1])
            for key, (present, meta) in self._entries.items()
        )


class Plugin(Generic[C], ABC):
    """A statically declared plugin with a typed configuration and async apply body."""

    NAME: str
    Config: TypeForm[C]

    @staticmethod
    def inject() -> Inject:
        """Return declared dependencies; the default plugin has none."""
        return Inject()

    @staticmethod
    def provide() -> list[str]:
        """Return provision metadata used for tooling and dependency-cycle warnings."""
        return []

    @staticmethod
    @abstractmethod
    async def apply(ctx: Context, config: C) -> None:
        """Create one plugin episode and collect its resources through the context."""


@dataclass(frozen=True)
class RuntimeKey:
    """Registry identity of a plugin class or a fresh closure definition."""

    kind: Literal["type", "definition"]
    value: type[object] | int


@dataclass(frozen=True)
class _Definition:
    """Immutable builder state; derived definitions retain their original registry key."""

    key: RuntimeKey
    name: str | None
    inject: Inject
    provide: tuple[Key, ...]
    tags: tuple[str, ...]
    decoder: Decoder
    apply: Apply


_definition_lock = threading.Lock()
_definition_counter: int = 0


class PluginDef:
    """A type-erased definition with copy-on-write-style builder operations."""

    def __init__(self, inner: _Definition) -> None:
        """Retain immutable definition state created by the supported factory methods."""
        self._inner: _Definition = inner

    @classmethod
    def of(cls, plugin: type[Plugin[C]]) -> PluginDef:
        """Build a typed definition whose key is stable across calls for the same class."""

        def decoder(value: JsonValue) -> object:
            """Decode the plugin's declared configuration at its entry boundary."""
            return decode(value, plugin.Config)

        def apply(context: Context, config: object) -> Awaitable[None]:
            """Pass the decoder's known result to its typed plugin without revalidating it."""
            return plugin.apply(context, cast(C, config))

        return cls(
            _Definition(
                RuntimeKey("type", plugin),
                plugin.NAME,
                plugin.inject().copy(),
                tuple(Key(key) for key in plugin.provide()),
                (),
                decoder,
                apply,
            )
        )

    @classmethod
    def new(cls, apply: Callable[[Context, JsonValue], Awaitable[None]]) -> PluginDef:
        """Create a fresh closure definition whose configuration is raw JSON."""

        def erased(context: Context, config: object) -> Awaitable[None]:
            """Clone the raw JSON at apply, matching the reference's Arc<Value> conversion."""
            return apply(context, deepcopy(cast(JsonValue, config)))

        return cls._fresh(decode_json, erased)

    @classmethod
    def typed(
        cls, apply: Callable[[Context, C], Awaitable[None]], decoder: Callable[[JsonValue], C]
    ) -> PluginDef:
        """Create a fresh typed closure definition using an explicit boundary decoder."""

        def erased(context: Context, config: object) -> Awaitable[None]:
            """Forward the known decoded type without a second validation pass."""
            return apply(context, cast(C, config))

        return cls._fresh(decoder, erased)

    @classmethod
    def _fresh(cls, decoder: Decoder, apply: Apply) -> PluginDef:
        """Allocate a process-wide fresh closure key."""
        global _definition_counter
        with _definition_lock:
            _definition_counter += 1
            key = RuntimeKey("definition", _definition_counter)
        return cls(_Definition(key, None, Inject(), (), (), decoder, apply))

    def name(self, name: str) -> PluginDef:
        """Return a renamed definition retaining its registry identity."""
        return PluginDef(replace(self._inner, name=name))

    def inject(self, inject: Inject | Iterable[str]) -> PluginDef:
        """Return a definition with its dependency declaration replaced."""
        specification = inject.copy() if isinstance(inject, Inject) else Inject(inject)
        return PluginDef(replace(self._inner, inject=specification))

    def provides(self, keys: Iterable[str]) -> PluginDef:
        """Replace provision metadata without restricting what apply may actually provide."""
        return PluginDef(replace(self._inner, provide=tuple(Key(key) for key in keys)))

    def tag(self, tag: str) -> PluginDef:
        """Append a host-recognized marker without changing registry identity."""
        return PluginDef(replace(self._inner, tags=(*self._inner.tags, tag)))

    def validate_with(self, validate: Callable[[JsonValue], JsonValue]) -> PluginDef:
        """Wrap decoding with a raw configuration transformation; latest wrappers run first."""
        previous = self._inner.decoder

        def decoder(value: JsonValue) -> object:
            """Transform a private JSON snapshot before invoking the existing decoder chain."""
            return previous(validate(deepcopy(value)))

        return PluginDef(replace(self._inner, decoder=decoder))

    def key(self) -> RuntimeKey:
        """Return the definition's stable registry identity."""
        return self._inner.key

    def plugin_name(self) -> str | None:
        """Return its optional explicit name."""
        return self._inner.name

    def inject_spec(self) -> Inject:
        """Return an independent dependency declaration snapshot."""
        return self._inner.inject.copy()

    def provide_spec(self) -> tuple[Key, ...]:
        """Return immutable declared provision metadata."""
        return self._inner.provide

    def has_tag(self, tag: str) -> bool:
        """Test whether this definition carries a particular host marker."""
        return tag in self._inner.tags

    def decode(self, value: JsonValue) -> object:
        """Run the configured decoder chain once."""
        return self._inner.decoder(value)

    def apply(self, context: Context, config: object) -> Awaitable[None]:
        """Invoke the apply factory with this definition's already-decoded configuration."""
        return self._inner.apply(context, config)

    def __repr__(self) -> str:
        """Describe the key and explicit name without exposing user callbacks."""
        return f"PluginDef(key={self.key()!r}, name={self.plugin_name()!r})"
