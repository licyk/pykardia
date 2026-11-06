"""Demonstrate file-to-runtime refresh and runtime-to-file writeback using a real include."""

import asyncio
import tempfile
from pathlib import Path

from pykardia import Context, JsonValue, catalog, plugin
from pykardia.fs import FsPlugin
from pykardia.include import Include
from pykardia.loader import Loader


@plugin(provide=["value"])
async def value(ctx: Context, config: JsonValue) -> None:
    """Expose the current configuration so file changes are visible through the runtime."""
    ctx.provide_as("value", config)


async def main() -> None:
    """Apply both edit directions in a temporary file and flush before resource cleanup."""
    with tempfile.TemporaryDirectory(prefix="pykardia-sync-") as directory:
        path = Path(directory) / "pykardia.yml"
        path.write_text("- id: value\n  name: value\n  config: 1\n")
        root = Context()
        try:
            plugins = catalog().register_def("value", value)
            await root.plugin(FsPlugin, None).settled()
            await root.plugin_def(Loader.plugin(plugins)).settled()
            loader = root.get(Loader).arc()
            await loader.create({"id": "file", "name": "include", "config": {"path": str(path)}})
            await loader.settled()
            tree = loader.resolve("file").subtree()
            assert tree is not None
            include = Include.of(tree)
            assert include is not None
            print("initial value:", root.view("value"))
            await tree.update("value", {"config": 2})
            await include.flush()
            print("runtime edit written to file:\n" + path.read_text())
            path.write_text("- id: value\n  name: value\n  config: 3\n")
            await include.refresh()
            await loader.settled()
            print("file edit applied to runtime:", root.view("value"))
        finally:
            await root.fiber().dispose()


if __name__ == "__main__":
    asyncio.run(main())
