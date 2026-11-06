"""The six observable fiber lifecycle states."""

from enum import Enum


class FiberState(Enum):
    """Lifecycle state independent of whether a transition is currently in flight."""

    Pending = "Pending"
    Loading = "Loading"
    Active = "Active"
    Unloading = "Unloading"
    Failed = "Failed"
    Disposed = "Disposed"
