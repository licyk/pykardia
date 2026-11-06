"""Console logging plugin with effect-owned exporter registration."""

from pykardia.core.context import Context
from pykardia.core.plugin import Plugin
from pykardia.logger_console.config import ConsoleConfig
from pykardia.logger_console.exporter import ConsoleExporter


class ConsolePlugin(Plugin[ConsoleConfig]):
    """Register a console exporter for one plugin episode."""

    NAME = "logger-console"
    Config = ConsoleConfig

    @staticmethod
    async def apply(ctx: Context, config: ConsoleConfig) -> None:
        """Collect exporter removal on the applying plugin's effect accumulator."""
        ctx.add_exporter(ConsoleExporter(config))
