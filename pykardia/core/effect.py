"""Effect collectors, cooperative setup and nested reversible resources."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pykardia.core.fiber import Fiber

from pykardia.core.cleanup import Disposal, Disposer, run_sequence
from pykardia.core.error import Error, Interrupted
from pykardia.core.runtime import Completion, Runtime


@dataclass
class EffectMeta:
    """An effect label and the nested effects it adopted."""

    label: str
    children: list[EffectMeta]


class EffectCell:
    """Own collected inverses and setup independently of a user's effect handle."""

    def __init__(self, runtime: Runtime, label: str) -> None:
        """Create an active collector before registering it on its owning fiber."""
        self.runtime = runtime
        self.label = label
        self._lock = threading.Lock()
        self._active = True
        self._closed = False
        self._detach: Callable[[], None] | None = None
        self._disposers: list[Disposer] = []
        self._children: list[EffectCell] = []
        self._setup: Completion[None] | None = None

    def set_detach(self, detach: Callable[[], None] | None) -> None:
        """Set or clear the owner's removal action without invoking it under the cell lock."""
        with self._lock:
            self._detach = detach

    def detach(self) -> None:
        """Remove this cell from its direct owner, at most once."""
        with self._lock:
            detach, self._detach = self._detach, None
        if detach is not None:
            detach()

    def is_active(self) -> bool:
        """Report whether disposal has started."""
        with self._lock:
            return self._active

    def push_disposer(self, disposer: Disposer) -> None:
        """Collect an inverse, or run it immediately after final cleanup closed the cell."""
        with self._lock:
            if not self._closed:
                self._disposers.append(disposer)
                return
        run_sequence(self.runtime, [disposer])

    def take_disposers(self, *, close: bool) -> list[Disposer]:
        """Drain collected inverses, optionally routing future additions to immediate cleanup."""
        with self._lock:
            self._closed |= close
            disposers, self._disposers = self._disposers, []
            return disposers

    def meta(self) -> EffectMeta:
        """Return a fresh recursive introspection snapshot."""
        with self._lock:
            children = tuple(self._children)
        return EffectMeta(self.label, [child.meta() for child in children])

    def ready(self) -> Completion[None]:
        """Return setup's shared outcome, or an immediate success for a sync effect."""
        with self._lock:
            return self._setup if self._setup is not None else Completion.ready(None)

    def dispose(self) -> Disposal:
        """Start cleanup once; let in-flight setup finish cooperatively before draining."""
        with self._lock:
            if not self._active:
                return Disposal(Completion.ready(None))
            self._active = False
            setup = self._setup
        self.detach()
        if setup is None:
            return run_sequence(self.runtime, self.take_disposers(close=True))
        return Disposal(self.runtime.spawn(self._dispose_after_setup(setup)))

    async def _dispose_after_setup(self, setup: Completion[None]) -> None:
        """Ignore the separately reported setup failure and finish all remaining inverses."""
        try:
            await setup
        except (Exception, asyncio.CancelledError):
            pass
        await run_sequence(self.runtime, self.take_disposers(close=True))

    def start(
        self,
        body: Callable[[EffectScope], Awaitable[None]],
        report: Callable[[BaseException], None],
    ) -> None:
        """Call the async factory now and keep its body alive until a cooperative boundary."""
        future = body(EffectScope(self))
        completion = self.runtime.spawn(self._run_setup(future, report))
        with self._lock:
            self._setup = completion

    async def _run_setup(
        self, future: Awaitable[None], report: Callable[[BaseException], None]
    ) -> None:
        """Clean up and report failures; interruption ends setup without reporting failure."""
        try:
            await future
        except Interrupted:
            return
        except (Exception, asyncio.CancelledError) as error:
            if isinstance(error, Error) and error.is_interrupted():
                return
            try:
                await run_sequence(self.runtime, self.take_disposers(close=False))
            except (Exception, asyncio.CancelledError) as cleanup:
                report(cleanup)
            report(error)
            raise


class EffectHandle:
    """A non-owning observer; dropping a handle does not dispose its effect."""

    def __init__(self, cell: EffectCell) -> None:
        """Retain the effect's state while its owner controls its lifetime."""
        self._cell = cell

    def label(self) -> str:
        """Return the label used when the effect was created."""
        return self._cell.label

    def is_active(self) -> bool:
        """Return whether cleanup has not yet started."""
        return self._cell.is_active()

    def dispose(self) -> Disposal:
        """Start cleanup immediately and return its cancellation-independent completion."""
        return self._cell.dispose()

    def ready(self) -> Completion[None]:
        """Observe setup's outcome, which is distinct from the cleanup result."""
        return self._cell.ready()

    def meta(self) -> EffectMeta:
        """Describe the effect and its adopted children."""
        return self._cell.meta()


class EffectScope:
    """The collector handed to an effect body, with explicit iteration boundaries."""

    def __init__(self, cell: EffectCell) -> None:
        """Bind the collector to one effect's lifetime."""
        self._cell = cell

    def defer(self, disposer: Disposer) -> None:
        """Collect an inverse without checking for interruption."""
        self._cell.push_disposer(disposer)

    def defer_fn(self, action: Callable[[], None]) -> None:
        """Collect a synchronous inverse."""
        self.defer(Disposer.new(action))

    def adopt(self, handle: EffectHandle | Fiber) -> None:
        """Move a nested effect from its direct owner into this collector's sequence."""
        if not isinstance(handle, EffectHandle):
            converted = handle.effect_handle()
            if converted is None:
                raise ValueError("only a live, non-root fiber has an instantiation effect")
            handle = converted
        handle._cell.detach()
        with self._cell._lock:
            self._cell._children.append(handle._cell)
        self.defer(Disposer.from_handle(handle))

    def checkpoint(self) -> None:
        """Raise Interrupted if disposal has superseded this setup."""
        if not self._cell.is_active():
            raise Interrupted()

    def yield_(self, disposer: Disposer) -> None:
        """Collect the inverse before checking for interruption."""
        self.defer(disposer)
        self.checkpoint()


def run_sync(runtime: Runtime, label: str, body: Callable[[EffectScope], None]) -> EffectHandle:
    """Execute a sync body; failures start cleanup before propagating to its caller."""
    cell = EffectCell(runtime, label)
    try:
        body(EffectScope(cell))
    except (Exception, asyncio.CancelledError):
        run_sequence(runtime, cell.take_disposers(close=True))
        raise
    return EffectHandle(cell)
