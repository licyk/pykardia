"""Public context derivation, effect registration and root-owned logging."""

from __future__ import annotations

import json
import weakref
from collections.abc import Awaitable, Callable, Iterable
from copy import deepcopy
from typing import TYPE_CHECKING, TypeVar, cast

from typing_extensions import TypeForm

from pykardia.core.cleanup import Disposer
from pykardia.core.config import ValidationError, decode
from pykardia.core.effect import EffectCell, EffectHandle, EffectScope, run_sync
from pykardia.core.error import Interrupted, TypeMismatch
from pykardia.core.events import (
    ConfigHook,
    Event,
    Listener,
    UpdateHook,
    dispatch_parallel,
    finish_listener,
    listeners_for,
)
from pykardia.core.fiber import Fiber
from pykardia.core.key import FiberId, ImplId, Key, RealmId
from pykardia.core.logger import Exporter, Logger, LoggerIntercept, Message
from pykardia.core.plugin import Inject, Plugin, PluginDef
from pykardia.core.registry import Registry, instantiate
from pykardia.core.root import Root
from pykardia.core.runtime import Completion
from pykardia.core.scope import InterceptScope, IsolateScope, Overlay, Scope, ScopeKind
from pykardia.core.service import Service, Svc, withdraw
from pykardia.core.state import Binding, Check, State, View
from pykardia.core.value import JsonValue, is_json, to_json

if TYPE_CHECKING:
    from pykardia.timer.service import TimerExt

T = TypeVar("T")
C = TypeVar("C")
Output = TypeVar("Output")
S = TypeVar("S", bound=Service)
E = TypeVar("E", bound=Event[object])


class Context:
    """A root, resource-owning fiber and independently derivable lookup scope."""

    def __init__(self) -> None:
        """Create an active root context without requiring an asyncio loop yet."""
        self._root: Root = Root()
        self._fiber: FiberId = FiberId.ROOT
        self._scope: Scope = self._root.read(lambda state: state.fibers[FiberId.ROOT].scope)

    @classmethod
    def new(cls) -> Context:
        """Create an independent root context."""
        return cls()

    @classmethod
    def _from_parts(cls, root: Root, identity: FiberId, scope: Scope) -> Context:
        """Construct another handle without allocating a new runtime or scope."""
        context = cls.__new__(cls)
        context._root, context._fiber, context._scope = root, identity, scope
        return context

    def fiber_id(self) -> FiberId:
        """Return the resource-owning fiber identity."""
        return self._fiber

    def is_root(self) -> bool:
        """Return whether effects created here belong to the root fiber."""
        return self._fiber == FiberId.ROOT

    def same_root(self, other: Context) -> bool:
        """Test whether both handles share a runtime."""
        return self._root is other._root

    def ptr_eq(self, other: Context) -> bool:
        """Test whether both handles denote the same derivation node."""
        return self._scope is other._scope

    def root(self) -> Context:
        """Return this runtime's root context, discarding derived lookup overlays."""
        scope = self._root.read(lambda state: state.fibers[FiberId.ROOT].scope)
        return Context._from_parts(self._root, FiberId.ROOT, scope)

    def fiber(self) -> Fiber:
        """Return the owning fiber with its own scope, rather than this derived scope."""
        return Fiber.of(self._root, self._fiber)

    def isolate(self, key: str) -> Context:
        """Derive a context assigning this key a fresh private realm."""
        return self.isolate_in(key, RealmId.new())

    def isolate_in(self, key: str, realm: RealmId) -> Context:
        """Derive a context assigning this key an explicitly shared realm."""
        return self._derive(IsolateScope(Key(key), realm))

    def intercept(self, key: str, meta: JsonValue) -> Context:
        """Derive a context carrying a snapshot of this key's interception metadata."""
        return self._derive(InterceptScope(Key(key), deepcopy(meta)))

    def extend(self) -> Context:
        """Derive a context with fresh mutable isolation and interception tables."""
        return self.extend_with(Overlay())

    def extend_with(self, overlay: Overlay) -> Context:
        """Derive a context sharing the supplied live overlay."""
        return self._derive(overlay)

    def overlay(self) -> Overlay | None:
        """Return the overlay on this exact node, if it was made by extend."""
        return self._scope.kind if isinstance(self._scope.kind, Overlay) else None

    def reparent(self, parent: Context) -> Context:
        """Move this node and descendants, returning a handle owned by the parent's fiber."""
        self._scope.set_parent(parent._scope)
        return Context._from_parts(self._root, parent._fiber, self._scope)

    def parent_overlay(self) -> Overlay | None:
        """Return only the immediate parent's overlay, if present."""
        parent = self._scope.parent()
        return parent.kind if parent is not None and isinstance(parent.kind, Overlay) else None

    def tag(self) -> int | None:
        """Return the nearest tagged overlay on the derivation chain."""
        return self._scope.tag()

    def is_within(self, ancestor: Context) -> bool:
        """Test whether this context is the ancestor itself or derived below it."""
        return self._scope.is_within(ancestor._scope)

    def isolated_keys(self) -> list[Key]:
        """List all isolation overrides on the chain, including repeated keys."""
        return self._scope.isolated_keys()

    def realm(self, key: str) -> RealmId | None:
        """Resolve the current realm if an override or root provision has assigned one."""
        return self._root.read(lambda state: state.realm_of(self._scope, key))

    def has_isolation(self, key: str) -> bool:
        """Test for an override rather than an inherited root realm."""
        return self._scope.own_realm(key) is not None

    def intercepts(self, key: str) -> list[JsonValue]:
        """Return metadata snapshots ordered from the outermost context inward."""
        return self._scope.intercepts(key)

    def intercept_meta(self, key: str, metadata_type: type[T]) -> T:
        """Decode merged metadata, defaulting the whole value when it is malformed."""
        try:
            return decode(merge_shallow(self.intercepts(key)), metadata_type)
        except ValidationError:
            return metadata_type()

    def plugin(self, plugin: type[Plugin[C]], config: C) -> Fiber:
        """Instantiate a typed plugin after serializing its source configuration."""
        return self.plugin_def(PluginDef.of(plugin), to_json(config))

    def plugin_value(self, plugin: type[Plugin[C]], config: JsonValue = None) -> Fiber:
        """Instantiate a typed plugin from raw JSON, decoding only when apply runs."""
        return self.plugin_def(PluginDef.of(plugin), config)

    def plugin_def(self, definition: PluginDef, config: JsonValue = None) -> Fiber:
        """Create a child fiber and collect its instantiation effect on this context."""
        return instantiate(self, definition, config)

    def inject(
        self, inject: Inject | Iterable[str], apply: Callable[[Context], Awaitable[None]]
    ) -> Fiber:
        """Create an anonymous closure plugin waiting for the declared dependencies."""
        return self.plugin_def(PluginDef.new(lambda context, config: apply(context)).inject(inject))

    def timer(self) -> TimerExt:
        """Return the checked timer service with this context as its scheduling caller."""
        from pykardia.timer.context import TimerContext

        return TimerContext.timer(self)

    def registry(self) -> Registry:
        """Return a handle to this root's ordered plugin registry."""
        return Registry(self)

    def provide(self, value: Service) -> EffectHandle:
        """Provide a typed service under its declared name."""
        return self.provide_as(value.NAME, value)

    def provide_arc(self, value: Service) -> EffectHandle:
        """Provide an already shared Python object under its declared name."""
        return self.provide(value)

    def provide_as(self, key: str, value: object) -> EffectHandle:
        """Provide the object under an explicit coeffect name."""
        return self.provide_checked(key, value)

    def provide_arc_as(self, key: str, value: object) -> EffectHandle:
        """Provide the shared object directly; Python references already have Arc ownership."""
        return self.provide_checked(key, value)

    def provide_checked(self, key: str, value: object, check: Check | None = None) -> EffectHandle:
        """Provide with a per-consumer availability predicate that must not reenter the runtime."""
        view: View | None = None
        if is_json(value):
            json_value = cast(JsonValue, value)

            def view() -> JsonValue:
                """Return a JSON snapshot without cloning the bound service itself."""
                return deepcopy(json_value)

        return self._bind_effect(key, value, check, view)

    def provide_serialized_as(
        self, key: str, value: T, serialize: Callable[[T], JsonValue] | None = None
    ) -> EffectHandle:
        """Expose a dynamic JSON view using a dataclass/enum or an explicit serializer."""

        def view() -> JsonValue:
            """Convert on every read, using null for serialization failures like serde_json."""
            try:
                return to_json(value) if serialize is None else deepcopy(serialize(value))
            except (TypeError, ValueError):
                return None

        return self._bind_effect(key, value, None, view)

    def _bind_effect(
        self, key: str, value: object, check: Check | None, view: View | None
    ) -> EffectHandle:
        """Register a provision and collect its two-phase withdrawal inverse."""

        def bind(scope: EffectScope) -> None:
            """Acquire the live slot before registering its cleanup."""
            identity = self._root.run(lambda state: state.bind(self, Key(key), value, check, view))
            scope.defer(withdraw(self._root, identity))

        return self.effect_sync(f"ctx.provide({json.dumps(key, ensure_ascii=False)})", bind)

    def deps(self, *services: type[Service]) -> tuple[Svc[Service], ...]:
        """Access a tuple of declared service types in the supplied order."""
        return tuple(self.get(service) for service in services)

    def get(self, service: type[S]) -> Svc[S]:
        """Access a service by its type's declared name through the committed view."""
        return self.get_as(service.NAME, service)

    def get_as(self, key: str, value_type: type[T]) -> Svc[T]:
        """Resolve checked access and require the exact concrete provided type."""
        identity, binding = self._root.read(lambda state: state.access(self, Key(key)))
        return self._wrap(Key(key), identity, binding, value_type)

    def view(self, key: str) -> JsonValue:
        """Read the checked binding's JSON view outside the state lock."""
        _, binding = self._root.read(lambda state: state.access(self, Key(key)))
        if binding.view is None:
            raise TypeMismatch(key, "a JSON view")
        return binding.view()

    def live(self, service: type[S]) -> Svc[S] | None:
        """Read an active typed binding reflectively without requiring inject."""
        return self.live_as(service.NAME, service)

    def live_as(self, key: str, value_type: type[T]) -> Svc[T] | None:
        """Read an active live binding, returning None for missing or mismatched types."""

        def resolve(state: State) -> tuple[ImplId, Binding] | None:
            """Snapshot one active live entry atomically."""
            identity = state.live_impl(self._scope, key, True)
            return None if identity is None else (identity, state.impls[identity])

        resolved = self._root.read(resolve)
        if resolved is None:
            return None
        try:
            return self._wrap(Key(key), *resolved, value_type)
        except TypeMismatch:
            return None

    def _wrap(self, key: Key, identity: ImplId, binding: Binding, value_type: type[T]) -> Svc[T]:
        """Preserve Rust's concrete downcast boundary, including bool versus int."""
        if type(binding.value) is not value_type:
            raise TypeMismatch(key, value_type.__qualname__)
        return Svc(cast(T, binding.value), self, key, identity, binding.provider)

    def migrate_binding(self, key: str, from_realm: RealmId, to_realm: RealmId) -> bool:
        """Move a free live slot without changing binding identity or restarting consumers."""

        def migrate(state: State) -> bool:
            """Perform the source-key and destination-slot check atomically."""
            identity = state.store.get(from_realm)
            if identity is None or state.impls[identity].key != key or to_realm in state.store:
                return False
            del state.store[from_realm]
            state.store[to_realm] = identity
            state.impls[identity].realm = to_realm
            return True

        return self._root.run(migrate)

    def refresh_consumers(self, key: str, realm: RealmId) -> None:
        """Re-resolve consumers after shared isolation or interception metadata changes."""
        self._root.run(lambda state: state.notify(Key(key), realm))

    def bindings(self, key: str) -> list[tuple[RealmId, ImplId]]:
        """List live bindings of a key in provision identity order."""
        return self._root.read(
            lambda state: sorted(
                (
                    (realm, identity)
                    for realm, identity in state.store.items()
                    if state.impls[identity].key == key
                ),
                key=lambda item: item[1],
            )
        )

    def binding_provider(self, key: str, realm: RealmId) -> Context | None:
        """Return the exact context that provided a matching live realm binding."""

        def resolve(state: State) -> Context | None:
            """Construct a context from captured fields without acquiring another lock."""
            identity = state.store.get(realm)
            if identity is None:
                return None
            binding = state.impls[identity]
            if binding.key != key:
                return None
            return Context._from_parts(self._root, binding.provider, binding.provider_scope)

        return self._root.read(resolve)

    def on(self, event_type: type[E], listener: Callable[[E], object]) -> EffectHandle:
        """Register a synchronous listener owned by this fiber."""
        return self._listen(event_type, listener, False, False)

    def on_prepend(self, event_type: type[E], listener: Callable[[E], object]) -> EffectHandle:
        """Register a synchronous listener before existing registrations."""
        return self._listen(event_type, listener, False, True)

    def on_async(
        self, event_type: type[E], listener: Callable[[E], Awaitable[object]]
    ) -> EffectHandle:
        """Register an asynchronous listener, awaited by serial and parallel dispatch."""
        return self._listen(event_type, listener, True, False)

    def once(self, event_type: type[E], listener: Callable[[E], object]) -> EffectHandle:
        """Remove a registration before invoking its callback so reentry cannot repeat it."""
        handle: EffectHandle | None = None
        called = False

        def invoke(event: E) -> object:
            """Consume the callback before any user code can dispatch the event again."""
            nonlocal called
            if called:
                return None
            called = True
            if handle is not None:
                handle.dispose()
            return listener(event)

        handle = self._listen(event_type, invoke, False, False)
        return handle

    def _listen(
        self,
        event_type: type[E],
        callback: Callable[[E], object],
        asynchronous: bool,
        prepend: bool,
    ) -> EffectHandle:
        """Install a removable record and collect removal as a synchronous inverse."""
        entry = Listener(self, cast(Callable[[Event[object]], object], callback), asynchronous)
        erased_type = cast(type[Event[object]], event_type)

        def register(scope: EffectScope) -> None:
            """Insert and remove the same record without dropping callbacks under the lock."""

            def insert(state: State) -> None:
                """Preserve prepend or append ordering in the actual listener list."""
                entries = state.listeners.setdefault(erased_type, [])
                entries.insert(0 if prepend else len(entries), entry)

            def remove() -> None:
                """Publish removal to dispatch snapshots and unlink this registration."""

                def unlink(state: State) -> None:
                    """Remove the entry and drop the empty event bucket."""
                    entry.removed = True
                    entries = state.listeners.get(erased_type, [])
                    if entry in entries:
                        entries.remove(entry)
                    if not entries:
                        state.listeners.pop(erased_type, None)

                self._root.run(unlink)

            self._root.run(insert)
            scope.defer_fn(remove)

        return self.effect_sync(f"ctx.on({event_type.__name__})", register)

    def emit(self, event: Event[Output]) -> None:
        """Call synchronous listeners now and start asynchronous listeners without awaiting."""
        erased = cast(Event[object], event)
        for entry in listeners_for(self, erased):
            if entry.removed:
                continue
            if entry.asynchronous:
                self._root.driver.spawn(
                    finish_listener(cast(Awaitable[object], entry.callback(erased)))
                )
            else:
                try:
                    entry.callback(erased)
                except Exception as error:
                    self.logger().log_error(error)

    def bail(self, event: Event[Output]) -> Output | None:
        """Stop at the first synchronous bailing output while starting asynchronous callbacks."""
        erased = cast(Event[object], event)
        for entry in listeners_for(self, erased):
            if entry.removed:
                continue
            output = entry.callback(erased)
            if entry.asynchronous:
                self._root.driver.spawn(finish_listener(cast(Awaitable[object], output)))
            elif event.is_bailed(cast(Output, output)):
                return cast(Output, output)
        return None

    async def serial(self, event: Event[Output]) -> Output | None:
        """Await listeners in registration order until one bails."""
        erased = cast(Event[object], event)
        for entry in listeners_for(self, erased):
            if entry.removed:
                continue
            output = entry.callback(erased)
            if entry.asynchronous:
                output = await cast(Awaitable[object], output)
            if event.is_bailed(cast(Output, output)):
                return cast(Output, output)
        return None

    async def parallel(self, event: Event[Output]) -> None:
        """Run all listeners and raise AggregateError containing every observed failure."""
        await dispatch_parallel(self, cast(Event[object], event))

    def listener_count(self, event_type: type[E]) -> int:
        """Count currently registered listeners regardless of event filtering."""
        return self._root.read(
            lambda state: len(state.listeners.get(cast(type[Event[object]], event_type), []))
        )

    def on_update(self, config_type: TypeForm[C], accept: Callable[[C], bool]) -> EffectHandle:
        """Offer matching decoded updates, including raw JSON; True keeps the current episode."""
        return self.on_update_with(
            config_type, lambda config: Completion.ready(None) if accept(config) else None
        )

    def on_update_with(
        self, config_type: TypeForm[C], accept: Callable[[C], Awaitable[None] | None]
    ) -> EffectHandle:
        """Register an update hook that may take ownership of completion without restarting."""

        def offer(config: object) -> Awaitable[None] | None:
            """Match the decoded concrete configuration or a raw JSON Value declaration."""
            matches = type(config) is config_type or (
                config_type == JsonValue
                and type(config) in (str, int, float, bool, list, dict, type(None))
            )
            return accept(cast(C, config)) if matches else None

        hook = UpdateHook(offer)

        def register(scope: EffectScope) -> None:
            """Collect a hook with removal observable by already captured update snapshots."""
            identity = self._root.run(
                lambda state: state.fibers[self._fiber].update_hooks.insert(hook)
            )

            def remove() -> None:
                """Mark the hook removed and unlink it if the fiber still exists."""
                hook.removed = True
                removed = self._root.run(
                    lambda state: (
                        state.fibers[self._fiber].update_hooks.remove(identity)
                        if self._fiber in state.fibers
                        else None
                    )
                )
                del removed

            scope.defer_fn(remove)

        return self.effect_sync("ctx.on_update()", register)

    def on_config(self, hook: ConfigHook) -> EffectHandle:
        """Register a root-wide configuration resolution hook in outermost-first order."""

        def register(scope: EffectScope) -> None:
            """Retain the hook until its owning context is disposed."""
            identity = self._root.run(lambda state: state.config_hooks.insert(hook))

            def remove() -> None:
                """Release the hook after leaving the critical section."""
                removed = self._root.run(lambda state: state.config_hooks.remove(identity))
                del removed

            scope.defer_fn(remove)

        return self.effect_sync("ctx.on_config()", register)

    def effect_sync(self, label: str, body: Callable[[EffectScope], None]) -> EffectHandle:
        """Run setup now, cleaning collected resources immediately if the body fails."""
        self._root.run(lambda state: state.assert_active(self._fiber))
        handle = run_sync(self._root.driver, label, body)
        self._root.attach(self._fiber, handle._cell)
        return handle

    def effect(self, label: str, body: Callable[[EffectScope], Awaitable[None]]) -> EffectHandle:
        """Start owned setup that survives dropped observers and stops cooperatively."""
        self._root.run(lambda state: state.assert_active(self._fiber))
        cell = EffectCell(self._root.driver, label)

        def report(error: BaseException) -> None:
            """Resolve current logger metadata when the asynchronous failure occurs."""
            self.logger().log_error(error)

        cell.start(body, report)
        self._root.attach(self._fiber, cell)
        return EffectHandle(cell)

    def on_dispose(self, action: Callable[[], None]) -> EffectHandle:
        """Register a synchronous inverse directly on this fiber."""
        return self.effect_sync("ctx.on_dispose()", lambda scope: scope.defer_fn(action))

    def defer(self, label: str, disposer: Disposer) -> EffectHandle:
        """Register an arbitrary inverse directly on this fiber."""
        return self.effect_sync(label, lambda scope: scope.defer(disposer))

    def checkpoint(self) -> None:
        """Report when this fiber's active load has been superseded or retired."""
        current = self._root.read(
            lambda state: (
                self._fiber in state.fibers
                and not state.fibers[self._fiber].removed
                and (
                    state.fibers[self._fiber].episode is None
                    or state.fibers[self._fiber].episode == state.fibers[self._fiber].target
                )
            )
        )
        if not current:
            raise Interrupted()

    def logger(self) -> Logger:
        """Resolve this context's logger name and level through its metadata."""
        return Logger.for_caller(self, self._root.read(lambda state: state.fiber_name(self._fiber)))

    def logger_named(self, name: str) -> Logger:
        """Use an explicit name while retaining the context's verbosity override."""
        meta = self.intercept_meta("logger", LoggerIntercept)
        return Logger(self._root, name, meta.level, self._fiber)

    def add_exporter(self, exporter: Exporter) -> EffectHandle:
        """Register an exporter whose inverse removes exactly this registration."""
        reference = weakref.ref(self._root)

        def register(scope: EffectScope) -> None:
            """Install the sink and collect its inverse on the current fiber."""
            state = self._root.logger
            with state.lock:
                identity = state.exporters.insert(exporter)

            def remove() -> None:
                """Drop the sink after releasing the logger lock."""
                root = reference()
                if root is not None:
                    with root.logger.lock:
                        removed = root.logger.exporters.remove(identity)
                    del removed

            scope.defer_fn(remove)

        return self.effect_sync("ctx.logger.exporter()", register)

    def log_buffer(self) -> list[Message]:
        """Return recent messages in their buffered order."""
        with self._root.logger.lock:
            return list(self._root.logger.buffer)

    def set_log_buffer_size(self, size: int) -> None:
        """Resize retained history immediately; zero disables buffering."""
        if size < 0:
            raise ValueError("log buffer size must be nonnegative")
        state = self._root.logger
        with state.lock:
            state.buffer_size = size
            while len(state.buffer) > size:
                state.buffer.popleft()

    def _derive(self, kind: ScopeKind) -> Context:
        """Keep runtime and resource ownership while allocating a fresh lookup node."""
        return Context._from_parts(self._root, self._fiber, Scope.child(self._scope, kind))

    def __repr__(self) -> str:
        """Describe the current owning fiber's logger fallback name."""
        return f"Context <{self.fiber().name()}>"


def merge_shallow(values: Iterable[JsonValue]) -> dict[str, JsonValue]:
    """Merge object fields right-biased, ignoring nonobjects and copying JSON ownership."""
    merged: dict[str, JsonValue] = {}
    for value in values:
        if isinstance(value, dict):
            merged.update(deepcopy(value))
    return merged
