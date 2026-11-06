"""Three-stage module reconciliation with shared-unit commit and cache restoration."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from pykardia.core.fiber import Fiber
from pykardia.core.plugin import PluginDef, RuntimeKey
from pykardia.hmr.graph import ExportKind, ModuleId

if TYPE_CHECKING:
    from pykardia.hmr.hmr import Hmr


@dataclass
class ImportFailure:
    """The sorted modules restored for an aborted unit and its first import failure."""

    modules: list[ModuleId]
    error: Exception


@dataclass
class ReconcileResult:
    """Surviving settled fibers, aborted import units and changed host modules."""

    settled_fibers: list[Fiber] = field(default_factory=list)
    import_failures: list[ImportFailure] = field(default_factory=list)
    skipped_host_modules: list[ModuleId] = field(default_factory=list)


@dataclass
class Stale:
    """One registered plugin's outer exporter, aliases and old module closure."""

    url: ModuleId
    urls: list[ModuleId]
    plugin: PluginDef
    modules: set[ModuleId]


@dataclass
class Attempt:
    """A stale plugin's replacement or failure and newly introduced cache records."""

    stale: Stale
    loaded: list[ModuleId]
    replacement: PluginDef | Exception


@dataclass
class Unit:
    """Connected replacement attempts that share evicted modules and commit together."""

    attempts: list[int] = field(default_factory=list)
    modules: set[ModuleId] = field(default_factory=set)


async def transaction(hmr: Hmr, callback: Callable[[], Awaitable[list[Path]]]) -> ReconcileResult:
    """Import replacement units, restore aborted caches, replace definitions and settle fibers."""
    result = ReconcileResult()
    roots: set[Path] = set()
    for root in await callback():
        if not root.is_absolute():
            raise ValueError(f"invalid root {root}, expected an absolute path")
        roots.add(normalize(root))
    if not roots:
        return result
    graph = hmr.graph()
    hosts = graph._host_ids()
    changed: set[ModuleId] = set()
    for id in graph.loaded():
        path = Path(id)
        if not any(ancestor in roots for ancestor in (path, *path.parents)):
            continue
        if id in hosts:
            result.skipped_host_modules.append(id)
        else:
            changed.add(id)
    if not changed:
        return result

    loaded = graph.loaded()
    old_edges = {id: graph.edges(id) for id in loaded}
    accepted, declined = analyze(loaded, old_edges, hosts, changed)
    registry = hmr._ctx.registry()
    exporters: dict[RuntimeKey, list[tuple[ModuleId, PluginDef]]] = {}
    for id in loaded:
        if id in declined:
            continue
        exports = graph.exports(id)
        if exports is None or exports.kind is not ExportKind.PLUGIN:
            continue
        definition = exports.plugin_def
        assert definition is not None
        runtime = registry.get(definition)
        if runtime is not None:
            exporters.setdefault(runtime, []).append((id, definition))

    def old(id: str) -> list[ModuleId]:
        """Read the frozen old graph before cache mutation."""
        return old_edges.get(id, [])

    stale_plugins: list[Stale] = []
    evicted = set(accepted)
    for exported in exporters.values():
        own = {id for id, _ in exported}
        closures = [
            closure([id], old, lambda url, own=own: url in declined and url not in own)
            for id, _ in exported
        ]
        modules = set().union(*closures)
        if not modules & accepted:
            continue
        evicted.update(modules)
        outermost = next(
            (
                i
                for i, (id, _) in enumerate(exported)
                if not any(j != i and id in reached for j, reached in enumerate(closures))
            ),
            0,
        )
        url, definition = exported[outermost]
        stale_plugins.append(Stale(url, [id for id, _ in exported], definition, modules))

    backups = {id: graph._evict(id) for id in evicted}
    retained = set(graph.loaded())
    attempts: list[Attempt] = []
    for stale in stale_plugins:
        before = set(graph.loaded())
        replacement: PluginDef | Exception
        try:
            for url in stale.urls:
                if url != stale.url:
                    graph.import_(url)
            exports = graph.import_(stale.url)
            if exports.kind is not ExportKind.PLUGIN:
                raise ValueError(f"invalid plugin at {stale.url}, expect a plugin export")
            assert exports.plugin_def is not None
            replacement = exports.plugin_def
        except Exception as error:
            hmr._ctx.logger().log_error(error)
            replacement = error
        introduced = [id for id in graph.loaded() if id not in before and id not in evicted]
        attempts.append(Attempt(stale, introduced, replacement))

    units = partition(attempts, evicted, graph.edges, retained)
    aborted, committed = [], []
    for unit in units:
        target = (
            aborted
            if any(isinstance(attempts[i].replacement, Exception) for i in unit.attempts)
            else committed
        )
        target.append(unit)
    restored = {id for unit in aborted for id in unit.modules}
    failures = [set(unit.modules) for unit in aborted]
    owned = {id for unit in units for id in unit.modules}
    for id in evicted - owned:
        reached = closure([id], old, lambda url: url not in evicted)
        if any(unit.modules & reached for unit in committed):
            continue
        for index, unit in enumerate(aborted):
            if unit.modules & reached:
                restored.add(id)
                failures[index].add(id)

    for attempt in attempts:
        for id in attempt.loaded:
            reached = closure([id], graph.edges, lambda url: url in retained)
            if reached & restored:
                graph._evict(id)
    for id in restored:
        if id in backups:
            graph._restore(id, backups[id])
    for unit, modules in zip(aborted, failures, strict=True):
        failure = next(
            attempts[i].replacement
            for i in unit.attempts
            if isinstance(attempts[i].replacement, Exception)
        )
        assert isinstance(failure, Exception)
        result.import_failures.append(ImportFailure(sorted(modules), failure))

    applied = {index for unit in committed for index in unit.attempts}
    fibers: list[Fiber] = []
    for index, attempt in enumerate(attempts):
        if isinstance(attempt.replacement, Exception) or index not in applied:
            continue
        replaced = registry.replace(attempt.stale.plugin, attempt.replacement)
        if replaced:
            hmr._ctx.logger().info(f"reload plugin at {attempt.stale.url}")
            fibers.extend(replaced)
    hmr._settling.update(fiber.id() for fiber in fibers)
    await asyncio.gather(*(fiber.settled() for fiber in fibers), return_exceptions=True)
    hmr._settling.clear()
    result.settled_fibers = [fiber for fiber in fibers if not fiber.is_removed()]
    return result


def analyze(
    loaded: list[ModuleId],
    edges: dict[ModuleId, list[ModuleId]],
    hosts: set[ModuleId],
    changed: set[ModuleId],
) -> tuple[set[ModuleId], set[ModuleId]]:
    """Find the reverse dependency closure without following edges out of host modules."""
    importers: dict[ModuleId, list[ModuleId]] = {}
    for id, deps in edges.items():
        if id not in hosts:
            for dep in deps:
                importers.setdefault(dep, []).append(id)
    accepted: set[ModuleId] = set()
    queue = list(changed)
    while queue:
        id = queue.pop()
        if id not in accepted:
            accepted.add(id)
            queue.extend(importers.get(id, []))
    return accepted, hosts | (set(loaded) - accepted)


def closure(
    roots: Iterable[ModuleId],
    edges: Callable[[str], list[ModuleId]],
    stop: Callable[[str], bool],
) -> set[ModuleId]:
    """Traverse reachable modules, excluding stopped nodes and their outgoing edges."""
    reached: set[ModuleId] = set()
    queue = list(roots)
    while queue:
        id = queue.pop()
        if id not in reached and not stop(id):
            queue.extend(edges(id))
            reached.add(id)
    return reached


def partition(
    attempts: list[Attempt],
    evicted: set[ModuleId],
    new: Callable[[str], list[ModuleId]],
    retained: set[ModuleId],
) -> list[Unit]:
    """Join attempts sharing evicted dependencies in either their old or new graph."""
    reaches = [
        (closure(attempt.stale.urls, new, lambda url: url in retained) | attempt.stale.modules)
        & evicted
        for attempt in attempts
    ]
    parents = list(range(len(attempts)))

    def find(index: int) -> int:
        """Find and compress a component's parent links."""
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    owners: dict[ModuleId, int] = {}
    for index, reach in enumerate(reaches):
        for id in reach:
            if id in owners:
                parents[find(index)] = find(owners[id])
            else:
                owners[id] = index
    units: dict[int, Unit] = {}
    for index, reach in enumerate(reaches):
        unit = units.setdefault(find(index), Unit())
        unit.attempts.append(index)
        unit.modules.update(reach)
    return list(units.values())


def normalize(path: Path) -> Path:
    """Collapse components with Rust PathBuf::pop semantics, including leading parents."""
    normal = Path("/" if path.anchor == "//" else path.anchor)
    for part in path.parts:
        if part == path.anchor:
            continue
        normal = normal.parent if part == ".." else normal / part
    return normal
