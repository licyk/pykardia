"""Filesystem service access with watcher effects attributed to the caller."""

import os
from dataclasses import dataclass
from types import NoneType

from pykardia.core.context import Context
from pykardia.core.plugin import Plugin
from pykardia.core.service import Service, Svc
from pykardia.fs.filter import Filter
from pykardia.fs.watcher import Watcher


class Fs(Service):
    """The filesystem watch coeffect."""

    NAME = "fs"


class FsPlugin(Plugin[None]):
    """Provide the filesystem service with unit configuration."""

    NAME = "fs"
    Config = NoneType

    @staticmethod
    def provide() -> list[str]:
        """Declare the provided filesystem service."""
        return [Fs.NAME]

    @staticmethod
    async def apply(ctx: Context, config: None) -> None:
        """Install the service on this plugin episode."""
        ctx.provide(Fs())


@dataclass
class WatchOptions:
    """Select directory descendants with ordered patterns or a predicate."""

    filter: Filter | None = None


class FsExt(Svc[Fs]):
    """Expose watching through a checked, caller-attributed service handle."""

    def __init__(self, service: Svc[Fs]) -> None:
        """Keep the service binding and original resource-owning caller."""
        super().__init__(
            service.arc(), service.caller(), service.key(), service.impl_id(), service._provider
        )

    def watch(self, path: str | os.PathLike[str], options: WatchOptions | None = None) -> Watcher:
        """Start a file or recursive directory watch owned by this handle's caller."""
        return Watcher(self.caller(), path, options.filter if options is not None else None)
