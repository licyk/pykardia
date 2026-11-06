"""Explicit convenience access corresponding to Rust's TimerContext extension trait."""

from pykardia.core.context import Context
from pykardia.timer.service import Timer, TimerExt


class TimerContext:
    """Access the caller-attributed timer service through an existing context."""

    @staticmethod
    def timer(context: Context) -> TimerExt:
        """Apply the ordinary checked dependency access before exposing timer operations."""
        return TimerExt(context.get(Timer))
