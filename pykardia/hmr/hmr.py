"""Caller-aware HMR service and serialized transaction entry point."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

from pykardia.core.config import decode
from pykardia.core.context import Context
from pykardia.core.key import FiberId
from pykardia.core.plugin import PluginDef
from pykardia.core.service import Service, Svc
from pykardia.hmr.graph import ModuleGraph
from pykardia.hmr.transaction import ReconcileResult, normalize, transaction
from pykardia.loader import Loader

_TRANSACTION: ContextVar[asyncio.Task[object] | None] = ContextVar(
    "pykardia_hmr_transaction", default=None
)


@dataclass
class WatchConfig:
    """Native watch selection and the quiet interval before a reload batch."""

    enabled: bool = False
    files: list[str] = field(default_factory=lambda: [".", "!**/.*", "!**/target"])
    debounce: Annotated[int, "u64"] = 100


@dataclass
class HmrConfig:
    """A base relative to the including source directory and optional native watching."""

    base: str | None = None
    watch: WatchConfig = field(default_factory=WatchConfig)


class Hmr(Service):
    """Reload modules through transactions owned by this service instance."""

    NAME = "hmr"

    def __init__(self, graph: ModuleGraph, base: Path, ctx: Context) -> None:
        """Retain graph, providing context and the transaction queue."""
        self._graph = graph
        self._base = base
        self._ctx = ctx
        self._queue = asyncio.Lock()
        self._settling: set[FiberId] = set()

    @staticmethod
    def plugin(graph: ModuleGraph) -> PluginDef:
        """Build the HMR provider for an existing host module graph."""

        async def apply(ctx: Context, config: HmrConfig) -> None:
            """Resolve the entry-relative base, provide HMR and optionally start its watcher."""
            loader = ctx.live(Loader)
            entry = loader.arc().entry_of(ctx) if loader is not None else None
            directory = entry.parent().tree().base_dir() if entry is not None else Path()
            base = normalize(Path.cwd() / directory / (config.base or "."))
            ctx.provide(Hmr(graph, base, ctx))
            if config.watch.enabled and config.watch.files:
                from pykardia.hmr.watch import start

                watcher = start(ctx, base, config.watch)
                try:
                    await watcher.settled()
                except Exception:
                    pass

        return (
            PluginDef.typed(apply, lambda value: decode(value, HmrConfig))
            .name("hmr")
            .provides([Hmr.NAME])
        )

    def graph(self) -> ModuleGraph:
        """Return the service's shared module graph."""
        return self._graph

    def base_dir(self) -> Path:
        """Return the normalized absolute directory used for watch paths and log display."""
        return self._base

    def _reloading(self, ctx: Context) -> bool:
        """Check whether the caller or an ancestor is settling in the running transaction."""
        if not self._settling:
            return False
        fiber = ctx.fiber()
        while fiber is not None:
            if fiber.id() in self._settling:
                return True
            if fiber.is_root():
                break
            fiber = fiber.parent()
        return False


class HmrExt(Svc[Hmr]):
    """Transaction operations retaining the accessing caller's identity."""

    def __init__(self, service: Svc[Hmr]) -> None:
        """Preserve the original handle's caller, provider and binding."""
        super().__init__(
            service.arc(), service.caller(), service.key(), service.impl_id(), service._provider
        )

    async def reconcile(self, roots: Callable[[], Awaitable[list[Path]]]) -> ReconcileResult:
        """Run the roots callback inside one serialized, non-nestable reload transaction."""
        hmr = self.arc()
        task = asyncio.current_task()
        if (_TRANSACTION.get() is task and task is not None) or hmr._reloading(self.caller()):
            raise ValueError("cannot start a transaction inside another transaction")
        async with hmr._queue:
            token = _TRANSACTION.set(task)
            try:
                return await transaction(hmr, roots)
            finally:
                _TRANSACTION.reset(token)
