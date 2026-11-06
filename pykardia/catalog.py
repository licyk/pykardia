"""Unified catalog for built-in plugins and their original Cordis package aliases."""

from pykardia.fs import FsPlugin
from pykardia.include import IncludePlugin
from pykardia.loader import Catalog, builtins
from pykardia.logger_console import ConsolePlugin
from pykardia.timer import TimerPlugin


def catalog() -> Catalog:
    """Create a fresh catalog with all built-ins and no implicit HMR module graph."""
    result = builtins()
    result.register(TimerPlugin).alias("@cordisjs/plugin-timer", "timer")
    result.register(ConsolePlugin).alias("@cordisjs/plugin-logger-console", "logger-console")
    result.register(FsPlugin).alias("@cordisjs/plugin-fs-node", "fs")
    result.register(IncludePlugin).alias("@cordisjs/plugin-include", "include")
    return result
