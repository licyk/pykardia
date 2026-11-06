"""Runtime-dispatched file watches owned by the accessing fiber."""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TypeVar

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers.api import BaseObserver

from pykardia.core.cleanup import Disposal, Disposer
from pykardia.core.context import Context
from pykardia.core.effect import EffectScope
from pykardia.core.id_table import IdTable
from pykardia.fs.filter import Filter, Predicate, admits, compile
from pykardia.fs.native import observer

T = TypeVar("T")


class Subscription:
    """An independent listener registration, removed explicitly rather than on collection."""

    def __init__(self, remove: Callable[[], object]) -> None:
        """Retain one idempotent removal callback."""
        self._remove = remove

    def remove(self) -> None:
        """Stop this registration without affecting other copies of its callback."""
        self._remove()


class EventHandler(FileSystemEventHandler):
    """Forward native events to the event loop without running user callbacks on threads."""

    def __init__(self, watcher: Watcher, physical: Path, logical: Path) -> None:
        """Retain the watch whose loop owns dispatch."""
        self.watcher = watcher
        self.physical = physical
        self.logical = logical

    def on_any_event(self, event: FileSystemEvent) -> None:
        """Retain create/modify/rename notifications and ignore access and deletion."""
        if event.event_type not in ("created", "modified", "moved"):
            return
        for raw in (
            (event.src_path, event.dest_path) if event.event_type == "moved" else (event.src_path,)
        ):
            if raw:
                path = self.logical / Path(os.fsdecode(raw)).relative_to(self.physical)
                self.watcher._post(self.watcher._changed, path)


class Watcher:
    """A real native watch whose signals and listeners run in order on its asyncio loop."""

    def __init__(self, ctx: Context, path: str | os.PathLike[str], filter: Filter | None) -> None:
        """Normalize the target, start watching its directory and collect the inverse."""
        self._ctx = ctx
        self._path = Path(os.path.abspath(path))
        self._is_file = not self._path.is_dir()
        self._base = self._path.parent if self._is_file else self._path
        self._keep: Predicate | None = compile(filter) if isinstance(filter, list) else filter
        self._lock = threading.RLock()
        self._change: IdTable[Callable[[Path], None]] = IdTable()
        self._ready: IdTable[Callable[[], None]] = IdTable()
        self._error: IdTable[Callable[[Exception], None]] = IdTable()
        self._loop = asyncio.get_running_loop()
        self._active = True
        self._observer: BaseObserver = observer(lambda error: self._post(self._error_signal, error))
        self._physical = self._base.resolve()
        self._handler = EventHandler(self, self._physical, self._base)
        self._links: set[Path] = set()

        def setup(scope: EffectScope) -> None:
            """Start actual OS monitoring and register deterministic shutdown."""
            self._observer.schedule(self._handler, str(self._physical), recursive=not self._is_file)
            scope.defer(Disposer.future(self._close))
            self._observer.start()
            if not self._is_file:
                self._scan_links(self._base, set())

        self._handle = ctx.effect_sync("ctx.fs.watch()", setup)

    def context(self) -> Context:
        """Return the caller context that owns this watch."""
        return self._ctx

    def dispose(self) -> Disposal:
        """Stop delivery immediately and wait for native threads to finish."""
        return self._handle.dispose()

    def on_change(self, listener: Callable[[Path], None]) -> Subscription:
        """Subscribe to changed absolute file paths below the target."""
        with self._lock:
            identity = self._change.insert(listener)
        return self._subscription(self._change, identity)

    def on_ready(self, listener: Callable[[], None]) -> Subscription:
        """Schedule this listener once after registration, never in the registering call."""
        with self._lock:
            identity = self._ready.insert(listener)
        self._post(self._ready_signal, identity)
        return self._subscription(self._ready, identity)

    def on_error(self, listener: Callable[[Exception], None]) -> Subscription:
        """Handle native watch errors; absence of listeners causes a warning log."""
        with self._lock:
            identity = self._error.insert(listener)
        return self._subscription(self._error, identity)

    def _subscription(self, table: IdTable[T], identity: int) -> Subscription:
        """Remove only this ID while retaining source callback snapshot semantics."""

        def remove() -> None:
            """Unregister under the shared hooks lock."""
            with self._lock:
                table.remove(identity)

        return Subscription(remove)

    def _post(self, callback: Callable[[T], None], arg: T) -> None:
        """Transfer a signal from any watch thread to the owning loop."""
        with self._lock:
            if self._active:
                self._loop.call_soon_threadsafe(callback, arg)

    def _changed(self, path: Path) -> None:
        """Handle directory links and apply file or traversal filtering on the runtime."""
        if not self._active:
            return
        if path.is_dir():
            if not self._is_file and path.is_symlink():
                self._watch_link(path)
            return
        if self._is_file:
            kept = path == self._path
        else:
            relative = str(path.relative_to(self._base)).replace("\\", "/")
            kept = self._keep is None or admits(self._keep, relative, False)
        if kept:
            self._emit(self._change, path)

    def _ready_signal(self, identity: int) -> None:
        """Call a still-registered ready listener exactly once for its queued ID."""
        with self._lock:
            listener = self._ready.get(identity) if self._active else None
        if listener is not None:
            listener()

    def _error_signal(self, error: Exception) -> None:
        """Deliver a native error or warn when no error listener is present."""
        if not self._active:
            return
        with self._lock:
            unhandled = self._error.is_empty()
        if unhandled:
            self._ctx.logger().warn(error)
        self._emit(self._error, error)

    def _emit(self, table: IdTable[Callable[[T], None]], arg: T) -> None:
        """Call starting IDs in order, excluding listeners removed during this dispatch."""
        with self._lock:
            identities = [identity for identity, _ in table.iter()]
        for identity in identities:
            with self._lock:
                listener = table.get(identity)
            if listener is not None:
                listener(arg)

    def _watch_link(self, path: Path) -> None:
        """Add a directory link because watchdog does not recursively traverse symlinks."""
        if path in self._links:
            return
        try:
            physical = path.resolve()
            self._observer.schedule(
                EventHandler(self, physical, path), str(physical), recursive=True
            )
        except OSError:
            return
        self._links.add(path)

    def _scan_links(self, path: Path, visited: set[Path]) -> None:
        """Follow directory links at setup while visiting each real directory tree once."""
        real = path.resolve()
        if real in visited:
            return
        visited.add(real)
        for root, dirs, _ in os.walk(path):
            for name in dirs:
                child = Path(root) / name
                if child.is_symlink() and child.is_dir():
                    self._watch_link(child)
                    self._scan_links(child, visited)

    def _close(self) -> Awaitable[None]:
        """Disable queued delivery before joining the observer outside the event loop."""
        with self._lock:
            self._active = False

        async def join() -> None:
            """Stop and join every native watcher thread before disposal completes."""
            await asyncio.to_thread(self._observer.stop)
            if self._observer.ident is not None:
                await asyncio.to_thread(self._observer.join)

        return join()
