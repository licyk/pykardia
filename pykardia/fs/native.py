"""Native observers supporting independent watches and runtime error delivery."""

import os
import sys
from collections.abc import Callable

from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver


def observer(on_error: Callable[[Exception], None]) -> BaseObserver:
    """Select independent native watches and forward Linux reader failures."""
    if sys.platform == "darwin":
        from watchdog.observers.kqueue import KqueueObserver

        # FSEvents shares path registrations across independent observers.
        return KqueueObserver()
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
