"""Native HMR watch batches with serialized retries of failed changed files."""

import asyncio
import json
from pathlib import Path

from pykardia.core.context import Context
from pykardia.core.fiber import Fiber
from pykardia.fs import Fs, FsExt, WatchOptions
from pykardia.hmr.hmr import Hmr, HmrExt, WatchConfig


def start(ctx: Context, base: Path, config: WatchConfig) -> Fiber:
    """Install a native watch beneath an fs injection owned by the HMR provider."""
    ctx.logger().info(f"watching {json.dumps(config.files, ensure_ascii=False)} in {base}")

    async def apply(child: Context) -> None:
        """Queue native events and retire the consumer when this watch is disposed."""
        hmr = HmrExt(child.get(Hmr))
        watcher = FsExt(child.get(Fs)).watch(base, WatchOptions(filter=config.files))
        changes: asyncio.Queue[Path | None] = asyncio.Queue()

        def changed(path: Path) -> None:
            """Record an actual file change and wake the batch consumer."""
            hmr.logger().debug(f"change detected at {relative(base, path)}")
            changes.put_nowait(path)

        watcher.on_change(changed)
        child.on_dispose(lambda: changes.put_nowait(None))
        child._root.driver.spawn(batches(hmr, base, changes, config.debounce / 1000))

    return ctx.inject([Fs.NAME], apply)


async def batches(
    hmr: HmrExt,
    base: Path,
    changes: asyncio.Queue[Path | None],
    debounce: float,
) -> None:
    """Combine each quiet-window batch with the previous batch's failed paths."""
    retained: set[Path] = set()
    while (first := await changes.get()) is not None:
        files, retained = retained, set()
        files.add(first)
        while True:
            try:
                path = (
                    changes.get_nowait()
                    if not changes.empty()
                    else await asyncio.wait_for(changes.get(), debounce)
                )
            except asyncio.TimeoutError:
                break
            if path is None:
                return
            files.add(path)
        retained = await flush(hmr, base, files)


async def flush(hmr: HmrExt, base: Path, files: set[Path]) -> set[Path]:
    """Reload one batch and retain only its failed paths, or all paths on transaction failure."""
    logger = hmr.logger()
    logger.debug(f"reconcile {len(files)} changed files")

    async def roots() -> list[Path]:
        """Capture the changed paths inside the transaction."""
        return list(files)

    try:
        result = await hmr.reconcile(roots)
    except Exception as error:
        logger.warn(error)
        return files
    if result.skipped_host_modules:
        lines = [f"- {relative(base, Path(id))}" for id in result.skipped_host_modules]
        logger.warn("skipped host modules:\n" + "\n".join(lines))
    return {
        Path(id)
        for failure in result.import_failures
        for id in failure.modules
        if Path(id) in files
    }


def relative(base: Path, path: Path) -> Path:
    """Show paths below the watch base relatively, preserving outside paths as supplied."""
    return path.relative_to(base) if path.is_relative_to(base) else path
