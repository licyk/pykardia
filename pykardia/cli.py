"""Run a live file-backed pykardia application until Ctrl-C."""

import asyncio
import signal
import sys
from collections.abc import Awaitable
from pathlib import Path
from types import FrameType

from pykardia.catalog import catalog
from pykardia.core.context import Context
from pykardia.core.plugin import PluginDef
from pykardia.fs import FsPlugin
from pykardia.hmr import DylibSource, Hmr, ModuleCode, ModuleGraph, ModuleSource, WasmSource
from pykardia.loader import Catalog, Loader, Resolver
from pykardia.logger_console import ConsoleConfig, ConsoleExporter


def main() -> int:
    """Start with the first path argument, defaulting to pykardia.yml."""
    return asyncio.run(start(sys.argv[1] if len(sys.argv) > 1 else "pykardia.yml"))


async def start(path: str) -> int:
    """Report startup or cleanup errors through the app logger and return its exit status."""
    root = Context()
    status = 0
    try:
        await run(root, path)
    except Exception as error:
        root.logger_named("app").log_error(error)
        status = 1
    try:
        await root.fiber().dispose()
    except Exception as error:
        root.logger_named("app").log_error(error)
        status = 1
    return status


async def run(root: Context, path: str) -> None:
    """Boot console, fs, loader and watched include in source order, then await Ctrl-C."""
    root.add_exporter(ConsoleExporter(ConsoleConfig()))
    root.plugin(FsPlugin, None)
    plugins = catalog()
    graph = ModuleGraph(GuestFiles())
    plugins.register_def("hmr", Hmr.plugin(graph)).alias("@cordisjs/plugin-hmr", "hmr")
    resolver = Plugins(plugins, graph, Path(path).absolute().parent)
    await root.plugin_def(Loader.plugin(resolver)).settled()
    loader = root.get(Loader).arc()
    loader.set_logs(True)
    id = await loader.create({"name": "include", "config": {"path": path, "watch": True}})
    fiber = loader.resolve(id).inner_fiber()
    if fiber is not None and (error := fiber.error()) is not None:
        raise error
    await interrupted()


async def interrupted() -> None:
    """Wait for Ctrl-C or Windows Ctrl-Break and restore the previous signal handlers."""
    loop = asyncio.get_running_loop()
    stopped = asyncio.Event()
    signals = (signal.SIGINT, signal.SIGBREAK) if sys.platform == "win32" else (signal.SIGINT,)
    previous = {number: signal.getsignal(number) for number in signals}

    def stop(number: int, frame: FrameType | None) -> None:
        """Wake the event loop even when it is waiting on Windows I/O."""
        loop.call_soon_threadsafe(stopped.set)

    for number in signals:
        signal.signal(number, stop)
    try:
        await stopped.wait()
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


class Plugins(Resolver):
    """Resolve built-in names before loading guest paths relative to the root config file."""

    def __init__(self, plugins: Catalog, graph: ModuleGraph, base: Path) -> None:
        """Retain a shared catalog, module graph and lexical base directory."""
        self.catalog, self.graph, self.base = plugins, graph, base

    def resolve(self, name: str) -> Awaitable[PluginDef]:
        """Snapshot built-ins or import the corresponding guest module."""
        if self.catalog.get(name) is not None:
            return self.catalog.resolve(name)
        return self.graph.resolve(str(self.base / name))


class GuestFiles(ModuleSource):
    """Select WASM for .wasm/.wat and the native C ABI for other file extensions."""

    def __init__(self) -> None:
        """Retain one shared WASM engine and a native loader."""
        self.wasm, self.dylib = WasmSource(), DylibSource()

    def load(self, id: str) -> ModuleCode:
        """Load the selected real backend without treating executables as CLI process guests."""
        source = self.wasm if Path(id).suffix in (".wasm", ".wat") else self.dylib
        return source.load(id)
