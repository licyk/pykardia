"""Native observers supporting independent watches and runtime error delivery."""

import os
import sys
import time
from collections.abc import Callable

from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver


def observer(on_error: Callable[[Exception], None]) -> BaseObserver:
    """Select independent native watches and forward Linux reader failures."""
    if sys.platform == "darwin":
        from watchdog.observers.fsevents import FSEventsEmitter, _fsevents

        class IndependentEmitter(FSEventsEmitter):
            """Keep native stream identities separate for watches of the same path."""

            def run(self) -> None:
                """Register this emitter as the stream key and forward native errors."""
                self._start_time = time.monotonic()
                try:
                    # watchdog's process-wide registry otherwise keys streams by path.
                    _fsevents.add_watch(self, self, self.events_callback, [self.watch.path])
                    _fsevents.read_events(self)
                except Exception as error:
                    on_error(error)

            def on_thread_stop(self) -> None:
                """Remove only this emitter's stream and stop its native run loop."""
                _fsevents.remove_watch(self)
                _fsevents.stop(self)

        return BaseObserver(IndependentEmitter)
    if sys.platform != "linux":
        return Observer()
    from watchdog.observers.inotify import InotifyEmitter
    from watchdog.observers.inotify_buffer import InotifyBuffer

    class ReportingBuffer(InotifyBuffer):
        """Keep watchdog's native event reader while reporting errors to its owner."""

        def run(self) -> None:
            """Report native read errors and close the reader's queue and file descriptor."""
            try:
                super().run()
            except OSError as error:
                on_error(error)
                self.stop()

    class ReportingEmitter(InotifyEmitter):
        """Use the reporting buffer without changing watchdog's event translation."""

        def on_thread_start(self) -> None:
            """Start the native reader with the emitter's original mask and recursion."""
            self._inotify = ReportingBuffer(
                os.fsencode(self.watch.path),
                recursive=self.watch.is_recursive,
                event_mask=self.get_event_mask_from_filter(),
            )

    return BaseObserver(ReportingEmitter)
