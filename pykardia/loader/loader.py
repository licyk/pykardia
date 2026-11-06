"""Declarative tree coordination, reactive entry lifecycles and managed realms."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable, Generator
from copy import deepcopy
from dataclasses import dataclass
from typing import TypeAlias

from pykardia.core.cleanup import Disposal
from pykardia.core.config import Issue, JsonValue, PathSegment, ValidationError
from pykardia.core.context import Context, merge_shallow
from pykardia.core.effect import EffectHandle, EffectScope
from pykardia.core.error import Error
from pykardia.core.events import ConfigCommitted, PluginRemoved, StatusChanged
from pykardia.core.fiber import Fiber
from pykardia.core.key import RealmId
from pykardia.core.plugin import Inject, PluginDef
from pykardia.core.runtime import Completion
from pykardia.core.service import Service
from pykardia.core.status import FiberState
from pykardia.core.value import json_equal
from pykardia.loader.backend import (
    MISSING,
    EntryChange,
    Location,
    MemoryTree,
    Missing,
    TreeBackend,
    value_at,
)
from pykardia.loader.catalog import ModuleNotFound, Resolver
from pykardia.loader.expr import ExprError, has_expr, interpolate_at
from pykardia.loader.handles import EntryHandle, GroupHandle, TreeHandle
from pykardia.loader.options import (
    EntryOptions,
    EntryValidationError,
    diff_keys,
    sort_keys,
    validate_options,
)
from pykardia.loader.state import GroupData, LoaderState, PatchHook, TreeData

OUTER_TAG = "loader:outer"
PatchNext: TypeAlias = Awaitable[None]


@dataclass
class RealmDiff:
    """An effective realm change and the old binding provider located inside the entry."""

    key: str
    old: RealmId | None
    new: RealmId | None
    inside: Context | None


class LazyPatch:
    """Construct the next patch coroutine only when a hook actually awaits it."""

    def __init__(self, next_: Callable[[], Awaitable[None]]) -> None:
        """Retain the continuation without creating an unawaited coroutine."""
        self._next = next_

    def __await__(self) -> Generator[object, None, None]:
        """Drive the rest of the hook chain lazily."""
        return self._next().__await__()


class Loader(Service):
    """The loader service maintaining entry source trees and their live plugin fibers."""

    NAME = "loader"

    def __init__(self, ctx: Context, resolver: Resolver, backend: TreeBackend) -> None:
        """Create an installed loader's initial in-memory records and root tree."""
        self._ctx = ctx
        self._resolver = resolver
        self._state = LoaderState()
        self._lock = threading.RLock()
        self._pending = 0
        self._next = 3
        self._logs = False
        tree_ctx = ctx.extend()
        self._state.groups[2] = GroupData(tree_ctx, 1)
        self._state.trees[1] = TreeData(tree_ctx, 2, backend)

    @staticmethod
    def plugin(resolver: Resolver) -> PluginDef:
        """Build a loader definition whose root tree uses memory-only persistence."""
        return Loader.plugin_with(resolver, MemoryTree())

    @staticmethod
    def plugin_with(resolver: Resolver, backend: TreeBackend) -> PluginDef:
        """Build a loader definition with a shared resolver and custom root backend."""

        async def apply(ctx: Context, config: JsonValue) -> None:
            """Install one independent loader service for the applying plugin episode."""
            loader = Loader(ctx, resolver, backend)
            loader.install()

        return PluginDef.new(apply).name("loader").provides([Loader.NAME])

    def install(self) -> None:
        """Register the service, await barrier and source synchronization event hooks."""

        def check(metadata: list[JsonValue]) -> bool:
            """Hold await consumers while initialization or its resulting loads remain pending."""
            merged = merge_shallow(metadata)
            return (
                not (isinstance(merged, dict) and merged.get("await") is True) or self._pending == 0
            )

        self._ctx.provide_checked(self.NAME, self, check)
        self._ctx.on(PluginRemoved, lambda event: self.on_removed(event.fiber))
        self._ctx.on(ConfigCommitted, lambda event: self.on_committed(event.fiber, event.config))
        self._ctx.on(StatusChanged, lambda event: self.on_status(event.fiber, event.new))

        def config_hook(
            fiber: Fiber, config: JsonValue, next_: Callable[[JsonValue], object]
        ) -> object:
            """Interpolate an entry's own configuration and locate source-originated failures."""
            source = deepcopy(config)
            try:
                if has_expr(config) and self.own_entry(fiber) is not None:
                    config = interpolate_at(fiber.context(), config, ["config"])
                return next_(config)
            except Exception as error:
                entry = self.own_entry(fiber)
                with self._lock:
                    data = self._state.entries.get(entry) if entry is not None else None
                    written = (
                        (data.parent, deepcopy(data.options))
                        if data is not None and json_equal(data.read().config, source)
                        else None
                    )
                if written is not None:
                    raise self.locate_error(*written, error) from None
                raise

        self._ctx.on_config(config_hook)

    def context(self) -> Context:
        """Return the loader service's definition context."""
        return self._ctx

    def set_logs(self, enabled: bool) -> None:
        """Enable or disable entry apply/unload messages."""
        self._logs = enabled

    def tree(self) -> TreeHandle:
        """Return the loader's root tree."""
        return TreeHandle(self, 1)

    def root(self) -> GroupHandle:
        """Return the loader's root entry group."""
        return self.tree().root()

    async def create(
        self, options: JsonValue, parent: str | None = None, position: int | None = None
    ) -> str:
        """Create an entry through the loader's root tree namespace."""
        return await self.tree().create(options, parent, position)

    def remove(self, id: str) -> None:
        """Remove an entry through the root tree namespace."""
        self.tree().remove(id)

    async def update(
        self,
        id: str,
        patch: JsonValue,
        parent: str | None | Missing = MISSING,
        position: int | None = None,
    ) -> None:
        """Patch or move an entry while preserving its live identity where possible."""
        await self.tree().update(id, patch, parent, position)

    def resolve(self, id: str) -> EntryHandle:
        """Resolve an entry in the root tree or a nested owned tree."""
        return self.tree().resolve(id)

    def entries(self) -> list[EntryHandle]:
        """Enumerate registered entries of all nested trees depth first."""
        return self.tree().entries()

    def is_busy(self) -> bool:
        """Report initialization work counted by the loader availability barrier."""
        return self._pending > 0

    async def settled(self) -> None:
        """Wait for pending initialization and every current entry fiber to reach quiescence."""
        while True:
            while self.is_busy():
                await asyncio.sleep(0)
            with self._lock:
                fibers = [
                    fiber
                    for data in self._state.entries.values()
                    for fiber in (data.outer, data.inner)
                    if fiber is not None
                ]
            for fiber in fibers:
                try:
                    await fiber.settled()
                except Exception:
                    pass
            await asyncio.sleep(0)
            with self._lock:
                busy = self.is_busy() or any(
                    data.init is not None
                    or any(
                        fiber is not None
                        and fiber.state() in (FiberState.Loading, FiberState.Unloading)
                        for fiber in (data.outer, data.inner)
                    )
                    for data in self._state.entries.values()
                )
            if not busy:
                return

    def next_id(self) -> int:
        """Allocate a monotonically increasing loader-internal identity."""
        with self._lock:
            identity = self._next
            self._next += 1
            return identity

    def create_tree(self, ctx: Context, backend: TreeBackend) -> TreeHandle:
        """Create a tree owned by the nearest entry on the supplied context chain."""
        tree, group = self.next_id(), self.next_id()
        owner = self.entry_of_context(ctx)
        tree_ctx = ctx.extend()
        with self._lock:
            self._state.groups[group] = GroupData(tree_ctx, tree, owner=owner)
            self._state.trees[tree] = TreeData(tree_ctx, group, backend, owner)
            if owner is not None and owner in self._state.entries:
                self._state.entries[owner].subtree = tree
                self._state.entries[owner].subgroup = group
        return TreeHandle(self, tree)

    def new_subgroup(self, ctx: Context) -> GroupHandle:
        """Create a group sharing its owning entry's tree."""
        owner = self.entry_of_context(ctx)
        identity = self.next_id()
        with self._lock:
            data = self._state.entries.get(owner) if owner is not None else None
            tree = self._state.groups[data.parent].tree if data is not None else 1
            self._state.groups[identity] = GroupData(ctx, tree, owner=owner)
            if data is not None:
                data.subgroup = identity
        return GroupHandle(self, identity)

    def entry_of(self, ctx: Context) -> EntryHandle | None:
        """Find the nearest entry owning a context on its derivation chain."""
        identity = self.entry_of_context(ctx)
        return EntryHandle(self, identity) if identity is not None else None

    def entry_of_context(self, ctx: Context) -> int | None:
        """Resolve the nearest overlay tag only if this loader owns its entry."""
        tag = ctx.tag()
        with self._lock:
            return tag if tag is not None and tag in self._state.entries else None

    def own_entry(self, fiber: Fiber) -> int | None:
        """Recognize an entry's own non-group fiber rather than plugins it creates itself."""
        ctx = fiber.context()
        entry = self.entry_of_context(ctx)
        with self._lock:
            data = self._state.entries.get(entry) if entry is not None else None
            return (
                entry
                if data is not None
                and not data.read().group
                and ctx.parent_overlay() is data.intercept
                else None
            )

    def commit(self, tree: int, change: EntryChange) -> None:
        """Notify a backend after mutation and outside the loader state lock."""
        with self._lock:
            data = self._state.trees.get(tree)
        if data is not None:
            data.backend.commit(change)

    def release_pending(self) -> None:
        """Release one initialization and recheck await consumers when the last one settles."""
        with self._lock:
            self._pending -= 1
            released = self._pending == 0
        realm = self._ctx.realm(self.NAME)
        if released and realm is not None:
            self._ctx.refresh_consumers(self.NAME, realm)

    def show_log(self, entry: int, action: str) -> None:
        """Emit optional lifecycle messages for non-group entries only."""
        if not self._logs:
            return
        with self._lock:
            data = self._state.entries.get(entry)
            options = data.read() if data is not None else None
        if options is not None and not options.group:
            self._ctx.root().logger_named("loader").info(f"{action} plugin {options.name}")

    def on_patch_context(self, ctx: Context, hook: PatchHook) -> EffectHandle:
        """Register a caller-owned wrapper around the entry context-patching continuation."""

        def setup(scope: EffectScope) -> None:
            """Collect exact registration removal on the caller's effect scope."""
            with self._lock:
                identity = self._state.patch_hooks.insert(hook)

            def remove() -> None:
                """Remove this hook without changing an already captured hook chain."""
                with self._lock:
                    self._state.patch_hooks.remove(identity)

            scope.defer_fn(remove)

        return ctx.effect_sync("loader.on_patch_context()", setup)

    async def group_create(self, group: int, options: JsonValue) -> str:
        """Create or adopt the tree's entry under a group, then reconcile its fibers."""
        issues = validate_options(options, False)
        if issues:
            identifier = options.get("id") if isinstance(options, dict) else None
            if isinstance(identifier, str):
                with self._lock:
                    tree = self._state.groups[group].tree
                    entry = self._state.trees[tree].store.pop(identifier, None)
                if entry is not None:
                    self.dispose_entry(entry)
                    self.partial_dispose(entry, self._state.entries[entry].options, False)
            raise EntryValidationError(issues)
        with self._lock:
            state = self._state
            tree = state.groups[group].tree
            identifier = state.ensure_id(tree, options)
            entry = state.trees[tree].store.get(identifier)
            if entry is None:
                entry = self.next_id()
                state.new_entry(entry, self._ctx, group)
                state.trees[tree].store[identifier] = entry
            data = state.entries[entry]
            data.parent = group
            legacy = deepcopy(data.options)
            data.options = deepcopy(options)
            state.set_entry_options(entry, options)
        await self.entry_update(entry, legacy, True)
        with self._lock:
            return self._state.composite_id(entry)

    def group_remove(self, group: int, id: str, is_dispose: bool) -> None:
        """Unregister an entry only if another group has not already adopted it."""
        with self._lock:
            state = self._state
            tree = state.groups[group].tree
            entry = state.trees[tree].store.get(id)
            if entry is None or state.entries[entry].parent != group:
                return
            del state.trees[tree].store[id]
        self.dispose_entry(entry)
        with self._lock:
            if not is_dispose:
                state.set_group_data(
                    group,
                    [item for item in state.groups[group].data if EntryOptions.read(item).id != id],
                )
            legacy = deepcopy(state.entries[entry].options)
        self.partial_dispose(entry, legacy, False)

    async def group_update(self, group: int, config: list[JsonValue]) -> None:
        """Reconcile old and new entry keys concurrently, reporting per-entry failures."""
        with self._lock:
            state = self._state
            target = state.groups.get(group)
            if target is None:
                return
            tree = target.tree
            keys: list[str | int] = [
                identifier
                for options in target.data
                if (identifier := EntryOptions.read(options).id)
            ]
            for index, options in enumerate(config):
                if isinstance(options, dict) and not validate_options(options, False):
                    identifier = state.ensure_id(tree, options)
                    if identifier not in keys:
                        keys.append(identifier)
                elif not (
                    isinstance(options, dict)
                    and isinstance(options.get("id"), str)
                    and options["id"] in keys
                ):
                    keys.append(index)
            state.set_group_data(group, config)

        async def reconcile(key: str | int) -> None:
            """Reconcile one source key without allowing its error to cancel sibling entries."""
            options = (
                config[key]
                if isinstance(key, int)
                else next(
                    (item for item in config if isinstance(item, dict) and item.get("id") == key),
                    MISSING,
                )
            )
            if isinstance(options, Missing):
                if isinstance(key, str):
                    self.group_remove(group, key, False)
            else:
                try:
                    await self.group_create(group, deepcopy(options))
                except Exception as error:
                    self._ctx.logger().log_error(self.locate_error(group, options, error))

        await asyncio.gather(*(reconcile(key) for key in keys))

    def group_stop(self, group: int) -> None:
        """Remove group registrations while keeping the data for later persistence."""
        with self._lock:
            data = self._state.groups.get(group)
            identifiers = (
                [EntryOptions.read(item).id for item in data.data] if data is not None else []
            )
        for identifier in identifiers:
            self.group_remove(group, identifier, True)

    def on_removed(self, fiber: Fiber) -> None:
        """Persist actual plugin self-disposal while ignoring loader or ancestor retirement."""
        with self._lock:
            state = self._state
            entry = state.inner_fibers.pop(fiber.id(), None)
            data = state.entries.get(entry) if entry is not None else None
            if data is None or entry is None:
                return
            tree_ctx = state.trees[state.groups[data.parent].tree].ctx
        if tree_ctx.fiber().is_removed():
            return
        self.show_log(entry, "unload")
        parent = fiber.parent()
        if parent is None or parent.state() in (FiberState.Unloading, FiberState.Disposed):
            return
        with self._lock:
            if data.disposing or not state.registered(entry):
                return
            legacy = deepcopy(data.options)
            options = deepcopy(legacy)
            if not isinstance(options, dict):
                return
            options["disabled"] = True
            sort_keys(options)
            data.verdict = True
            state.set_entry_options(entry, options)
            group, has_outer = data.parent, data.outer is not None
            tree = state.groups[group].tree
        self.commit(
            tree,
            EntryChange(
                EntryOptions.read(options).id, GroupHandle(self, group), None, options, legacy
            ),
        )
        if has_outer:
            self.dispose_entry(entry)

    def on_committed(self, fiber: Fiber, config: JsonValue) -> None:
        """Write a tracked plugin's own config update back to its source entry."""
        with self._lock:
            state = self._state
            entry = state.inner_fibers.get(fiber.id())
            data = state.entries.get(entry) if entry is not None else None
            if data is None or entry is None:
                return
            legacy = deepcopy(data.options)
            options = deepcopy(legacy)
            if not isinstance(options, dict):
                return
            options["config"] = deepcopy(config)
            sort_keys(options)
            state.set_entry_options(entry, options)
            group = data.parent
            tree = state.groups[group].tree
        self.commit(
            tree,
            EntryChange(
                EntryOptions.read(options).id, GroupHandle(self, group), None, options, legacy
            ),
        )

    def on_status(self, fiber: Fiber, new: FiberState) -> None:
        """Invalidate a dependency-holding outer fiber's cached verdict before it unloads."""
        if new is not FiberState.Unloading:
            return
        with self._lock:
            entry = self._state.outer_fibers.get(fiber.id())
            data = self._state.entries.get(entry) if entry is not None else None
            if data is not None and data.outer is not None and data.outer.id() == fiber.id():
                data.verdict = None

    async def entry_update(self, entry: int, legacy: JsonValue, force: bool) -> None:
        """Choose a joined rebuild or an in-place patch after source options change."""
        changed = self.resolve_inject(entry)
        with self._lock:
            blocked = self._state.blocked(entry)
        if changed is None or blocked:
            self.dispose_entry(entry)
            return
        with self._lock:
            data = self._state.entries.get(entry)
            if data is None:
                return
            options = deepcopy(data.options)
            live = data.outer is not None or data.inner_live()
        renamed = EntryOptions.read(options).name != EntryOptions.read(legacy).name
        if changed or renamed or not live:
            disposal = self.dispose_entry(entry) if live else None
            await self.init(entry, disposal)
            return
        diff = diff_keys(options, legacy)
        if not diff and not force:
            return
        self.partial_dispose(entry, legacy, True)
        await self.patch_context(entry, diff)

    def resolve_inject(self, entry: int) -> bool | None:
        """Evaluate entry injection metadata in its parent group's context."""
        with self._lock:
            data = self._state.entries.get(entry)
            if data is None:
                return None
            declared, parent = data.read().inject, self._state.groups[data.parent].ctx
        specification = Inject()
        try:
            for key in declared.keys():
                if declared.has_meta(key):
                    metadata = interpolate_at(parent, declared.meta(key), ["inject", str(key)])
                    specification = specification.with_meta(key, metadata)
                else:
                    specification = specification.with_(key)
        except Exception as error:
            self.report(entry, error)
            with self._lock:
                data.inject = Inject()
                data.verdict = True
            return None
        with self._lock:
            changed = specification != data.inject
            data.inject = specification
        return changed

    def dispose_entry(self, entry: int) -> Disposal | None:
        """Retire the top entry fiber, suppressing self-disposal persistence."""
        with self._lock:
            data = self._state.entries.get(entry)
            if data is None:
                return None
            fiber = data.outer if data.outer is not None else data.inner
            data.outer = None
            if fiber is None:
                return None
            self._state.outer_fibers.pop(fiber.id(), None)
        return self.dispose_fiber(entry, fiber)

    def dispose_fiber(self, entry: int, fiber: Fiber) -> Disposal:
        """Mark the synchronous retirement event as loader-driven."""
        with self._lock:
            data = self._state.entries.get(entry)
            if data is not None:
                data.disposing = True
        try:
            return fiber.dispose()
        finally:
            with self._lock:
                if data is not None:
                    data.disposing = False

    async def init(self, entry: int, disposal: Disposal | None) -> None:
        """Join one shared initialization task even when concurrent updates change its inputs."""
        with self._lock:
            data = self._state.entries.get(entry)
            if data is None:
                return
            if data.init is None:
                identity = self.next_id()
                task = self._ctx._root.driver.spawn(self.init_task(entry, disposal))
                data.init = identity, task
                self._pending += 1
                self._ctx._root.driver.spawn(self.settle_after(entry, task))
            else:
                identity, task = data.init
        try:
            await task
        except Exception:
            pass
        with self._lock:
            if data.init is not None and data.init[0] == identity:
                data.init = None

    async def settle_after(self, entry: int, task: Completion[None]) -> None:
        """Release the await barrier only after initialization and its resulting fibers settle."""
        try:
            try:
                await task
            except Exception:
                pass
            with self._lock:
                data = self._state.entries.get(entry)
                fibers = (data.outer, data.inner) if data is not None else ()
            for fiber in fibers:
                if fiber is not None:
                    try:
                        await fiber.settled()
                    except Exception:
                        pass
        finally:
            self.release_pending()

    async def init_task(self, entry: int, disposal: Disposal | None) -> None:
        """Discard stale imports and patches, re-reading the current name before creating fibers."""
        if disposal is not None:
            try:
                await disposal
            except Exception:
                pass
        while True:
            with self._lock:
                data = self._state.entries.get(entry)
                if data is None or data.outer is not None or data.inner_live():
                    return
                data.verdict = None
                name = data.read().name
            definition = await self.import_(entry, name)
            current = self.current_name(entry)
            if current is not None and current != name:
                continue
            if definition is None:
                return
            with self._lock:
                if not self._state.registered(entry) or self._state.blocked(entry):
                    return
            try:
                await self.patch_context(entry, [])
            except Exception as error:
                self.report(entry, error)
            current = self.current_name(entry)
            if current is not None and current != name:
                continue
            with self._lock:
                if not self._state.registered(entry) or self._state.blocked(entry):
                    return
                data.init = None
                ctx, specification = data.ctx, data.inject.copy()
            if specification.is_empty():
                self.start(entry, ctx, definition)
                return
            try:
                outer = ctx.plugin_def(self.outer_def(specification))
                with self._lock:
                    self._state.outer_fibers[outer.id()] = entry
                    data.outer = outer
            except Exception as error:
                self._ctx.logger().log_error(error)
            return

    def current_name(self, entry: int) -> str | None:
        """Read the latest entry name after an asynchronous boundary."""
        with self._lock:
            data = self._state.entries.get(entry)
            return data.read().name if data is not None else None

    async def import_(self, entry: int, name: str) -> PluginDef | None:
        """Resolve through a tree-specific resolver or the loader default and report failures."""
        with self._lock:
            data = self._state.entries.get(entry)
            backend = (
                self._state.trees[self._state.groups[data.parent].tree].backend
                if data is not None
                else None
            )
        resolver = backend.resolver() if backend is not None else None
        try:
            return await (resolver or self._resolver).resolve(name)
        except Exception as error:
            self.report(entry, error)
            return None

    def outer_def(self, specification: Inject) -> PluginDef:
        """Create an entry dependency holder that resolves again on each activation."""

        async def apply(ctx: Context, config: JsonValue) -> None:
            """Re-resolve after dependencies recover and checkpoint any interrupted activation."""
            entry = self.entry_of_context(ctx)
            if entry is None:
                return
            name = self.current_name(entry)
            if name is None:
                return
            definition = await self.import_(entry, name)
            ctx.checkpoint()
            if definition is not None:
                self.start(entry, ctx, definition)

        return PluginDef.new(apply).name(OUTER_TAG).tag(OUTER_TAG).inject(specification)

    def start(self, entry: int, ctx: Context, definition: PluginDef) -> None:
        """Evaluate admission and create the actual plugin under its entry interception overlay."""
        with self._lock:
            if not self._state.registered(entry):
                return
            admitted, failures = self._state.admit(entry, ctx)
            data = self._state.entries[entry]
            intercept, config = data.intercept, data.read().config
        for error in failures:
            self.report(entry, error)
        if not admitted:
            return
        self.show_log(entry, "apply")
        try:
            inner = ctx.extend_with(intercept).plugin_def(definition, config)
            with self._lock:
                self._state.inner_fibers[inner.id()] = entry
                data.inner = inner
        except Exception as error:
            self.report(entry, error)

    async def patch_context(self, entry: int, diff: list[str]) -> None:
        """Run caller hooks inside realm preparation and finish binding migration afterward."""
        table, diffs = self.isolate_prepare(entry)
        with self._lock:
            hooks = tuple(self._state.patch_hooks.values())
        try:
            await self.patch_chain(entry, hooks, 0, table, diff)
        finally:
            self.isolate_finish(diffs)

    async def patch_chain(
        self,
        entry: int,
        hooks: tuple[PatchHook, ...],
        index: int,
        table: dict[str, RealmId],
        diff: list[str],
    ) -> None:
        """Construct each continuation only when the preceding hook awaits it."""
        if index >= len(hooks):
            await self.patch_fibers(entry, table, diff)
            return
        continuation = LazyPatch(lambda: self.patch_chain(entry, hooks, index + 1, table, diff))
        await hooks[index](EntryHandle(self, entry), continuation)

    async def patch_fibers(self, entry: int, table: dict[str, RealmId], diff: list[str]) -> None:
        """Reparent the shared context and reconcile only a live admitted fiber episode."""
        with self._lock:
            data = self._state.entries.get(entry)
            if data is None:
                return
            parent = self._state.groups[data.parent].ctx
            data.ctx = data.ctx.reparent(parent)
            data.overlay.set_isolations(table)
            outer, inner_live, ctx = data.outer, data.inner_live(), data.ctx
        if outer is not None:
            if outer.state() is FiberState.Active:
                await self.reconcile(entry, outer.context(), diff)
        elif inner_live:
            await self.reconcile(entry, ctx, diff)

    async def reconcile(self, entry: int, ctx: Context, diff: list[str]) -> None:
        """Re-evaluate admission and apply the newest source config without stale writes."""
        with self._lock:
            data = self._state.entries.get(entry)
            if data is None:
                return
            inner = data.inner if data.inner_live() else None
            outer = data.outer
        if inner is None:
            if outer is not None:
                try:
                    await outer.restart()
                except Exception:
                    pass
            return
        with self._lock:
            admitted, failures = self._state.admit(entry, ctx)
            options = data.read()
        for error in failures:
            self.report(entry, error)
        if not admitted:
            self.dispose_fiber(entry, inner)
            return
        if "config" not in diff and not options.group:
            return
        config = options.config
        while True:
            settle = inner.update_with(config, True)
            with self._lock:
                current = self._state.entries.get(entry)
                latest = current.read().config if current is not None else MISSING
            if not isinstance(latest, Missing) and not json_equal(latest, config):
                config = latest
            else:
                await settle
                return

    def partial_dispose(self, entry: int, legacy: JsonValue, active: bool) -> None:
        """Release unreferenced shared realms from the entry's previous options."""
        with self._lock:
            self._state.collect_realms(entry, legacy, active)

    def isolate_prepare(self, entry: int) -> tuple[dict[str, RealmId], list[RealmDiff]]:
        """Compute the replacement isolation table and providers eligible for migration."""
        with self._lock:
            data = self._state.entries.get(entry)
            if data is None:
                return {}, []
            ctx, parent = data.ctx, self._state.groups[data.parent].ctx
            table = {
                name: self._state.realm(entry, name, label) for name, label in data.read().isolate
            }
            names = dict.fromkeys([*table, *ctx.isolated_keys(), *parent.isolated_keys()])
            diffs: list[RealmDiff] = []
            for key in names:
                old, new = ctx.realm(key), table.get(key) or parent.realm(key)
                if old == new:
                    continue
                inside = ctx.binding_provider(key, old) if old is not None else None
                if inside is not None and not inside.is_within(ctx):
                    inside = None
                diffs.append(RealmDiff(key, old, new, inside))
            return table, diffs

    def isolate_finish(self, diffs: list[RealmDiff]) -> None:
        """Migrate only free live slots whose provider still resolves to the new realm."""
        for diff in diffs:
            if (
                diff.old is not None
                and diff.new is not None
                and diff.inside is not None
                and diff.inside.realm(diff.key) == diff.new
            ):
                self._ctx.migrate_binding(diff.key, diff.old, diff.new)
            for realm in (diff.old, diff.new):
                if realm is not None:
                    self._ctx.refresh_consumers(diff.key, realm)

    def report(self, entry: int, error: Exception) -> None:
        """Log an entry failure with available source locations and ancestor frames."""
        with self._lock:
            data = self._state.entries.get(entry)
            source = (data.parent, deepcopy(data.options)) if data is not None else None
        self._ctx.logger().log_error(
            self.locate_error(*source, error) if source is not None else error
        )

    def locate_error(self, group: int, options: JsonValue, error: Exception) -> Exception:
        """Attach source locations only when at least one frame can actually be located."""
        identifier = options.get("id") if isinstance(options, dict) else None
        if not isinstance(identifier, str):
            return error
        with self._lock:
            chain = self._state.chain(group, identifier)
        if not chain:
            return error
        backend, identifier = chain[0]
        found = False

        def locate(path: list[PathSegment], value: JsonValue | Missing) -> Location | None:
            """Track whether any real source location was found."""
            nonlocal found
            location = backend.locate(identifier, path, value)
            found = found or location is not None
            return location

        concrete = error.as_std() if isinstance(error, Error) else error
        if isinstance(concrete, EntryValidationError):
            text = "invalid entry options:" + located_issues(concrete.issues, [], options, locate)
        elif isinstance(concrete, ValidationError):
            text = "invalid config:" + located_issues(concrete.issues, ["config"], options, locate)
        elif isinstance(concrete, ModuleNotFound):
            location = locate(["name"], value_at(options, ["name"]))
            text = f"{error} (at name, {location})" if location is not None else str(error)
        elif isinstance(concrete, ExprError):
            label = ".".join(map(str, concrete.path))
            location = backend.locate_expr(
                identifier, concrete.path, concrete.expr, concrete.offset
            )
            found = found or location is not None
            text = f"{error}\n    at {label}" + (f" ({location})" if location is not None else "")
        else:
            return error
        for backend, identifier in chain:
            location = backend.locate(identifier, [], MISSING)
            if location is not None:
                found = True
                text += f"\n    at {identifier} ({location})"
        return Error.msg(text) if found else error


def located_issues(
    issues: list[Issue],
    prefix: list[PathSegment],
    options: JsonValue,
    locate: Callable[[list[PathSegment], JsonValue | Missing], Location | None],
) -> str:
    """Render validation issues with their relative field paths and actual source locations."""
    lines: list[str] = []
    for issue in issues:
        path = [*prefix, *issue.path]
        location = locate(path, value_at(options, path))
        suffix = [issue.format_path()] if issue.path else []
        if location is not None:
            suffix.append(str(location))
        lines.append(f"\n  - {issue.message}" + (f" (at {', '.join(suffix)})" if suffix else ""))
    return "".join(lines)
