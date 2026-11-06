"""Effect-owned callback scheduling and cancellable asyncio timer tasks."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Coroutine
from typing import Generic, TypeVar

from pykardia.core.cleanup import Disposal
from pykardia.core.context import Context
from pykardia.core.effect import EffectHandle

A = TypeVar("A")


class Scheduled(Generic[A]):
    """A rate-limited callback that ignores invocations after disposal."""

    def __init__(
        self, context: Context, label: str, trigger: Callable[[A], asyncio.Task[None] | None]
    ) -> None:
        """Collect the scheduler inverse on the caller's fiber before accepting calls."""
        self._lock = threading.Lock()
        self._disposed = False
        self._timer: asyncio.Task[None] | None = None
        self._trigger = trigger
        self._handle: EffectHandle = context.effect_sync(
            label, lambda scope: scope.defer_fn(self._close)
        )

    def call(self, args: A) -> None:
        """Cancel the previous pending invocation and trigger with the latest arguments."""
        with self._lock:
            if self._disposed:
                return
            previous, self._timer = self._timer, None
        if previous is not None:
            abort(previous)
        timer = self._trigger(args)
        with self._lock:
            if self._disposed:
                if timer is not None:
                    abort(timer)
            else:
                self._timer = timer

    def dispose(self) -> Disposal:
        """Dispose the owning effect synchronously and return its completion."""
        return self._handle.dispose()

    def _close(self) -> None:
        """Stop the pending timer and permanently suppress future callback triggers."""
        with self._lock:
            self._disposed = True
            timer, self._timer = self._timer, None
        if timer is not None:
            abort(timer)


def spawn(body: Coroutine[object, object, None]) -> asyncio.Task[None]:
    """Start a task retained by its owning effect and report callback failures to the loop."""
    task = asyncio.create_task(body)
    task.add_done_callback(report)
    return task


def report(task: asyncio.Task[None]) -> None:
    """Report a task failure without converting it into a plugin lifecycle failure."""
    if not task.cancelled() and (error := task.exception()) is not None:
        task.get_loop().call_exception_handler(
            {"message": "timer task failed", "exception": error, "task": task}
        )


def abort(task: asyncio.Task[None]) -> None:
    """Cancel directly on the task's loop or marshal cancellation from another thread."""
    try:
        current = asyncio.get_running_loop()
    except RuntimeError:
        current = None
    if current is task.get_loop():
        task.cancel()
    else:
        task.get_loop().call_soon_threadsafe(task.cancel)
