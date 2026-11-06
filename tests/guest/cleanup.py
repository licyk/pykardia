"""A real guest whose disposal leaves an observable file outside the host process."""

from dataclasses import dataclass
from pathlib import Path

from typing_extensions import Self

from pykardia.guest import Guest, Host, JsonValue, Level, Meta


@dataclass
class CleanupConfig:
    """Destination of the disposal marker."""

    path: str


class Cleanup(Guest[CleanupConfig]):
    """Record actual disposal even after the root console exporter is gone."""

    Config = CleanupConfig

    def __init__(self, path: str) -> None:
        """Retain the external marker destination for this instance."""
        self.path = path

    @staticmethod
    def meta() -> Meta:
        """Export a plugin with no service dependencies."""
        return Meta.plugin("cleanup")

    @classmethod
    def apply(cls, host: Host, config: CleanupConfig, imports: dict[str, JsonValue]) -> Self:
        """Announce actual application before returning the disposable instance."""
        host.log(Level.Info, "cleanup ready")
        return cls(config.path)

    def dispose(self, host: Host) -> None:
        """Write the marker from the actual guest cleanup export."""
        Path(self.path).write_text("disposed 你好", encoding="utf-8")
