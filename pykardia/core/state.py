"""Root-owned registry, service bindings and the reactive fiber state machine."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from pykardia.core.effect import EffectCell
from pykardia.core.error import (
    AlreadyProvided,
    Error,
    Inactive,
    InactiveEffect,
    Interrupted,
    Undeclared,
)
from pykardia.core.events import (
    ConfigHook,
    Event,
    Listener,
    PluginInserted,
    PluginRemoved,
    Replaced,
    ServiceChanged,
    StatusChanged,
    UpdateHook,
)
from pykardia.core.id_table import IdTable
from pykardia.core.key import FiberId, ImplId, Key, RealmId
from pykardia.core.logger import Level, Logger
from pykardia.core.plugin import Inject, PluginDef, RuntimeKey
from pykardia.core.scope import FiberScope, Scope
from pykardia.core.status import FiberState
from pykardia.core.value import JsonValue

if TYPE_CHECKING:
    from pykardia.core.context import Context
    from pykardia.core.root import Root

Check = Callable[[list[JsonValue]], bool]
View = Callable[[], JsonValue]


@dataclass(frozen=True)
class Epoch:
    """A revision and the binding identities committed in declaration order."""

    revision: int
    providers: tuple[ImplId, ...] = ()


@dataclass(frozen=True)
class Transition:
    """A transition scheduled after the mutation segment releases its lock."""

    kind: Literal["reload", "unload"]
    fiber: FiberId


@dataclass
class FiberData:
    """One fiber's live resolution, immutable episode view and resource accumulator."""

    parent: FiberId
    scope: Scope
    runtime: RuntimeKey | None = None
    inject: Inject = field(default_factory=Inject)
    state: FiberState = FiberState.Pending
    config: JsonValue = None
    removed: bool = False
    revision: int = 0
    resolved: dict[Key, ImplId] = field(default_factory=dict)
    target: Epoch | None = None
    episode: Epoch | None = None
    committed: dict[Key, ImplId] | None = None
    inertia: bool = False
    error: BaseException | None = None
    provisions: list[ImplId] = field(default_factory=list)
    draining: bool = False
    withdrawn: list[ImplId] = field(default_factory=list)
    disposables: IdTable[EffectCell] = field(default_factory=IdTable)
    update_hooks: IdTable[UpdateHook] = field(default_factory=IdTable)
    retire: EffectCell | None = None

    def settled_state(self) -> FiberState:
        """Choose the resting state in the reference's precedence order."""
        if self.removed:
            return FiberState.Disposed
        if self.error is not None:
            return FiberState.Failed
        return FiberState.Active if self.target is not None else FiberState.Pending


@dataclass
class RuntimeData:
    """A registered definition and its fibers in instantiation order."""

    definition: PluginDef
    fibers: list[FiberId] = field(default_factory=list)


@dataclass
class Binding:
    """A live or withdrawn service retained while committed consumers still rely on it."""

    key: Key
    realm: RealmId
    provider: FiberId
    provider_scope: Scope
    value: object
    check: Check | None
    view: View | None
    dependents: set[FiberId] = field(default_factory=set)


@dataclass(frozen=True)
class Located:
    """An own provision or a declaration admitting access, possibly without a binding."""

    binding: ImplId | None
    declared: FiberId | None = None


class State:
    """Mutable state used only inside a runtime segment; callbacks run through actions."""

    def __init__(self) -> None:
        """Create the permanent root and initially empty registry and service store."""
        root = FiberData(FiberId.ROOT, Scope.root(), state=FiberState.Active)
        root.target = root.episode = Epoch(0)
        root.committed = {}
        self.fibers: dict[FiberId, FiberData] = {FiberId.ROOT: root}
        self.runtimes: dict[RuntimeKey, RuntimeData] = {}
        self.aliases: dict[RuntimeKey, PluginDef] = {}
        self.impls: dict[ImplId, Binding] = {}
        self.store: dict[RealmId, ImplId] = {}
        self.root_realms: dict[str, RealmId] = {}
        self.listeners: dict[type[Event[object]], list[Listener]] = {}
        self.config_hooks: IdTable[ConfigHook] = IdTable()
        self.next_fiber = 0
        self.next_impl = 0
        self.actions: list[Transition | Callable[[Root], None]] = []

    def assert_active(self, identity: FiberId) -> None:
        """Allow retired in-flight setup to collect inverses, but reject other stale contexts."""
        fiber = self.fibers.get(identity)
        if fiber is None or (fiber.removed and fiber.state is not FiberState.Loading):
            raise InactiveEffect()

    def detach_effect(self, identity: FiberId, entry: int) -> EffectCell | None:
        """Remove an accumulator cell for release outside the state lock."""
        fiber = self.fibers.get(identity)
        return None if fiber is None else fiber.disposables.remove(entry)

    def realm_of(self, scope: Scope, key: str) -> RealmId | None:
        """Prefer the nearest explicit realm to the lazily assigned root realm."""
        realm = scope.own_realm(key)
        return self.root_realms.get(key) if realm is None else realm

    def def_of(self, identity: FiberId) -> PluginDef | None:
        """Return the currently registered implementation for this fiber."""
        fiber = self.fibers.get(identity)
        runtime = self.runtimes.get(fiber.runtime) if fiber and fiber.runtime else None
        return None if runtime is None else runtime.definition

    def fiber_name(self, identity: FiberId) -> str:
        """Find the nearest named instantiating ancestor, falling back to root."""
        while (fiber := self.fibers.get(identity)) is not None:
            definition = self.def_of(identity)
            if definition is not None and definition.plugin_name() is not None:
                return str(definition.plugin_name())
            if identity == FiberId.ROOT:
                break
            identity = fiber.parent
        return "root"

    def emit(self, make: Callable[[Root], Event[object]]) -> None:
        """Queue event construction and dispatch outside the critical section."""
        self.actions.append(lambda root: root.context().emit(make(root)))

    def log(self, identity: FiberId, level: Level, error: BaseException) -> None:
        """Capture the provider name now and report after releasing the state lock."""
        name = self.fiber_name(identity)
        self.actions.append(
            lambda root: Logger.internal(root, name, identity).log_error_at(level, error)
        )

    def drop_later(self, value: object) -> None:
        """Keep arbitrary values alive until their finalizers may safely reenter the runtime."""
        self.actions.append(lambda root, held=value: None)

    def forward(self, definition: PluginDef) -> tuple[RuntimeKey, PluginDef]:
        """Follow replacement aliases to the currently selected implementation."""
        while definition.key() in self.aliases:
            definition = self.aliases[definition.key()]
        return definition.key(), definition

    def insert_fiber(self, parent: Context, definition: PluginDef, config: JsonValue) -> FiberId:
        """Register a child and synchronously compute its first target."""
        self.assert_active(parent._fiber)
        key, definition = self.forward(definition)
        self.runtimes.setdefault(key, RuntimeData(definition))
        self.next_fiber += 1
        identity = FiberId(self.next_fiber)
        scope = Scope.child(parent._scope, FiberScope(identity))
        inject = definition.inject_spec()
        self.fibers[identity] = FiberData(
            parent._fiber, scope, key, inject, config=deepcopy(config)
        )
        self.emit(lambda root: PluginInserted(root.fiber_at(identity, scope)))
        for name in inject.keys():
            self.check_impl(identity, name)
        self.runtimes[key].fibers.append(identity)
        self.refresh(identity)
        return identity

    def retire_fiber(self, identity: FiberId) -> None:
        """Release live slots immediately while committed consumers drain the old bindings."""
        fiber = self.fibers.get(identity)
        if fiber is None or fiber.removed:
            return
        fiber.removed = True
        scope = fiber.scope
        self.emit(lambda root: PluginRemoved(root.fiber_at(identity, scope)))
        if fiber.runtime is not None and fiber.runtime in self.runtimes:
            runtime = self.runtimes[fiber.runtime]
            runtime.fibers.remove(identity)
            if not runtime.fibers:
                self.drop_later(self.runtimes.pop(fiber.runtime))
        for binding in list(fiber.provisions):
            self.unbind(binding)
        self.set_target(identity, None)
        self.collect_removed(identity)

    def replace(self, old: PluginDef, new: PluginDef) -> list[FiberId]:
        """Move instances in place and forward future instantiations of the old key."""
        old_key, _ = self.forward(old)
        key = new.key()
        self.aliases.pop(key, None)
        runtime = self.runtimes.get(old_key)
        identities = [] if runtime is None else list(runtime.fibers)
        if old_key != key:
            self.drop_later(self.runtimes.pop(old_key, None))
            self.aliases[old_key] = new
        if not identities:
            return identities
        self.runtimes.setdefault(key, RuntimeData(new))
        for identity in identities:
            fiber = self.fibers[identity]
            if fiber.runtime != key:
                fiber.runtime = key
                self.runtimes[key].fibers.append(identity)
            fiber.inject = new.inject_spec()
            fiber.resolved.clear()
            scope = fiber.scope
            self.emit(lambda root, i=identity, s=scope: Replaced(root.fiber_at(i, s)))
            for name in fiber.inject.keys():
                self.check_impl(identity, name)
            fiber.error = None
            fiber.revision += 1
            self.refresh(identity)
        return identities

    def warn_cycle(self, start: FiberId) -> None:
        """Warn for a declared cycle without treating pending dependencies as failures."""
        path = [start]
        if self.find_cycle(start, start, path, set()):
            names = " -> ".join(self.fiber_name(identity) for identity in path)
            self.log(start, Level.Warn, Error.msg(f"dependency cycle: {names}"))

    def find_cycle(
        self, start: FiberId, at: FiberId, path: list[FiberId], visited: set[FiberId]
    ) -> bool:
        """Search declared provider metadata in registration order and the same realm."""
        consumer = self.fibers[at]
        for key in consumer.inject.keys():
            realm = self.realm_of(consumer.scope, key)
            for runtime in self.runtimes.values():
                if key not in runtime.definition.provide_spec():
                    continue
                for provider in runtime.fibers:
                    data = self.fibers.get(provider)
                    if data is None or data.removed or self.realm_of(data.scope, key) != realm:
                        continue
                    if provider == start:
                        path.append(start)
                        return True
                    if provider not in visited:
                        visited.add(provider)
                        path.append(provider)
                        if self.find_cycle(start, provider, path, visited):
                            return True
                        path.pop()
        return False

    def bind(
        self, context: Context, key: Key, value: object, check: Check | None, view: View | None
    ) -> ImplId:
        """Create one provision, rejecting retired providers and occupied realm slots."""
        fiber = self.fibers.get(context._fiber)
        if fiber is None or fiber.removed:
            raise InactiveEffect()
        if key not in self.root_realms:
            self.root_realms[key] = RealmId.new()
        realm = context._scope.own_realm(key) or self.root_realms[key]
        if realm in self.store:
            raise AlreadyProvided(key, self.fiber_name(self.impls[self.store[realm]].provider))
        self.next_impl += 1
        identity = ImplId(self.next_impl)
        self.impls[identity] = Binding(
            key, realm, context._fiber, context._scope, value, check, view
        )
        self.store[realm] = identity
        fiber.provisions.append(identity)
        if fiber.state is FiberState.Active:
            self.notify(key, realm)
        return identity

    def unbind(self, identity: ImplId) -> None:
        """Withdraw only this live slot, retaining its old committed view."""
        binding = self.impls.get(identity)
        if binding is not None and self.store.get(binding.realm) == identity:
            del self.store[binding.realm]
            self.notify(binding.key, binding.realm)

    def retire_provision(self, identity: ImplId) -> None:
        """Keep withdrawn provisions reachable by descendants until the provider drain ends."""
        binding = self.impls.get(identity)
        if binding is None:
            return
        fiber = self.fibers.get(binding.provider)
        if fiber is not None and fiber.draining:
            fiber.withdrawn.append(identity)
        else:
            self.remove_provision(binding.provider, identity)

    def remove_provision(self, provider: FiberId, identity: ImplId) -> None:
        """Remove a drained binding and release its value after unlocking."""
        fiber = self.fibers.get(provider)
        if fiber is not None:
            fiber.provisions = [item for item in fiber.provisions if item != identity]
        self.drop_later(self.impls.pop(identity, None))

    def live_impl(self, scope: Scope, key: str, strict: bool) -> ImplId | None:
        """Resolve a live store entry, optionally requiring its provider to be Active."""
        realm = self.realm_of(scope, key)
        identity = self.store.get(realm) if realm is not None else None
        if identity is not None and strict:
            fiber = self.fibers.get(self.impls[identity].provider)
            if fiber is None or fiber.state is not FiberState.Active:
                return None
        return identity

    def locate(self, context: Context, key: Key) -> Located | None:
        """Walk derivation fibers, stopping at re-isolation and nearest admitting inject."""
        realm = self.realm_of(context._scope, key)
        node: Scope | None = context._scope
        identity = context._fiber
        while (fiber := self.fibers.get(identity)) is not None:
            for provision in reversed(fiber.provisions):
                binding = self.impls.get(provision)
                if (
                    binding is not None
                    and binding.key == key
                    and self.realm_of(binding.provider_scope, key) == realm
                ):
                    return Located(provision)
            while node is not fiber.scope:
                if node is None or node.isolates(key) is not None:
                    return None
                node = node.parent()
            binding_id = (fiber.committed or {}).get(key)
            if binding_id is not None or fiber.inject.contains(key):
                return Located(binding_id, identity)
            if identity == FiberId.ROOT:
                return None
            node = fiber.scope.parent()
            parent = node.fiber() if node is not None else None
            if parent is None:
                return None
            identity = parent
        return None

    def access(self, context: Context, key: Key) -> tuple[ImplId, Binding]:
        """Resolve through the committed view; root alone has unchecked live access."""
        if context._fiber not in self.fibers:
            raise Inactive(key)
        if context.is_root():
            found = self.live_impl(context._scope, key, False)
        else:
            located = self.locate(context, key)
            if located is None:
                raise Undeclared(key)
            found = located.binding
        if found is None or found not in self.impls:
            raise Inactive(key)
        return found, self.impls[found]

    def check_impl(self, identity: FiberId, key: Key) -> None:
        """Resolve a declared dependency and evaluate its availability predicate once."""
        fiber = self.fibers[identity]
        found = self.live_impl(fiber.scope, key, True)
        if found is not None and (check := self.impls[found].check) is not None:
            metadata = [fiber.inject.meta(key)] if fiber.inject.has_meta(key) else []
            metadata.extend(fiber.scope.intercepts(key))
            if not check(metadata):
                found = None
        if found is None:
            fiber.resolved.pop(key, None)
        else:
            fiber.resolved[key] = found

    def refresh(self, identity: FiberId) -> None:
        """Compute a target only when all declared bindings are available."""
        fiber = self.fibers.get(identity)
        if fiber is None:
            return
        providers: list[ImplId] = []
        for key in fiber.inject.keys():
            found = fiber.resolved.get(key)
            if found is None:
                self.set_target(identity, None)
                return
            providers.append(found)
        self.set_target(identity, Epoch(fiber.revision, tuple(providers)))

    def notify(self, key: Key, realm: RealmId) -> None:
        """Refresh matching registered consumers before publishing ServiceChanged."""
        identities = [identity for runtime in self.runtimes.values() for identity in runtime.fibers]
        for identity in identities:
            fiber = self.fibers.get(identity)
            if (
                fiber is not None
                and fiber.inject.contains(key)
                and self.realm_of(fiber.scope, key) == realm
            ):
                self.check_impl(identity, key)
                self.refresh(identity)
        self.emit(lambda root: ServiceChanged(key, realm))

    def restart(self, identity: FiberId) -> None:
        """Bump the revision without clearing a stored apply failure."""
        self.assert_active(identity)
        self.fibers[identity].revision += 1
        self.refresh(identity)

    def set_target(self, identity: FiberId, target: Epoch | None) -> None:
        """Coalesce changes under inertia without starting concurrent transitions."""
        fiber = self.fibers[identity]
        if fiber.target == target or fiber.error is not None:
            return
        previous, fiber.target = fiber.target, target
        if fiber.inertia:
            return
        if target is not None and previous is None:
            self.begin_reload(identity)
        else:
            self.begin_unload(identity)

    def update_state(self, identity: FiberId, next_state: FiberState | None = None) -> None:
        """Notify consumers on entering or leaving Active, before any inverse runs."""
        fiber = self.fibers[identity]
        old = fiber.state
        new = next_state if next_state is not None else fiber.settled_state()
        fiber.state = new
        if old is new:
            return
        scope = fiber.scope
        self.emit(lambda root: StatusChanged(root.fiber_at(identity, scope), old, new))
        if old is not FiberState.Active and new is not FiberState.Active:
            return
        for provision in list(fiber.provisions):
            binding = self.impls[provision]
            if self.store.get(binding.realm) == provision:
                self.notify(binding.key, binding.realm)

    def begin_reload(self, identity: FiberId) -> None:
        """Commit dependencies and acquire their guards before scheduling apply."""
        fiber = self.fibers[identity]
        fiber.inertia = True
        fiber.episode = fiber.target
        fiber.committed = dict(fiber.resolved)
        for impl_id in fiber.committed.values():
            binding = self.impls.get(impl_id)
            if binding is not None and binding.provider != identity:
                binding.dependents.add(identity)
        self.actions.append(Transition("reload", identity))
        self.update_state(identity, FiberState.Loading)

    def begin_unload(self, identity: FiberId) -> None:
        """Leave Active synchronously while cleanup runs behind the provider guard."""
        self.fibers[identity].inertia = True
        self.actions.append(Transition("unload", identity))
        self.update_state(identity, FiberState.Unloading)

    def finish_reload(self, identity: FiberId, error: BaseException | None = None) -> None:
        """A stale failure is logged but cannot poison the latest target."""
        fiber = self.fibers[identity]
        interrupted = (
            isinstance(error, Interrupted) or isinstance(error, Error) and error.is_interrupted()
        )
        if error is not None and not interrupted:
            self.log(identity, Level.Error, error)
            if fiber.episode == fiber.target:
                fiber.error = error
                fiber.target = None
        if fiber.episode == fiber.target:
            fiber.inertia = False
            self.update_state(identity)
        else:
            self.begin_unload(identity)

    def can_drain(self, identity: FiberId) -> bool:
        """Hold the fiber guard until all committed consumers have completed cleanup."""
        fiber = self.fibers.get(identity)
        return fiber is None or all(
            not self.impls[item].dependents for item in fiber.provisions if item in self.impls
        )

    def start_drain(self, identity: FiberId) -> list[EffectCell]:
        """Take inverses newest first, clearing direct registrations before user callbacks."""
        fiber = self.fibers[identity]
        fiber.draining = True
        cells = fiber.disposables.drain_rev()
        for cell in cells:
            cell.set_detach(None)
        return cells

    def finish_unload(self, identity: FiberId) -> None:
        """Release committed dependency guards after all inverses have finished."""
        fiber = self.fibers[identity]
        fiber.draining = False
        fiber.episode = None
        withdrawn, fiber.withdrawn = fiber.withdrawn, []
        committed, fiber.committed = fiber.committed or {}, None
        for impl_id in withdrawn:
            self.remove_provision(identity, impl_id)
        for impl_id in committed.values():
            if impl_id in self.impls:
                self.impls[impl_id].dependents.discard(identity)
        if fiber.target is None:
            fiber.inertia = False
            self.update_state(identity)
            self.collect_removed(identity)
        else:
            self.begin_reload(identity)

    def collect_removed(self, identity: FiberId) -> None:
        """Collect only retired fibers with no transition still in flight."""
        fiber = self.fibers.get(identity)
        if fiber is not None and fiber.removed and not fiber.inertia:
            self.update_state(identity)
            self.drop_later(self.fibers.pop(identity))
