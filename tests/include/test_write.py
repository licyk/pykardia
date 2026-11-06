"""External rename failures and writes racing the source's retry window."""

import errno
import os
from pathlib import Path

from pykardia import Level
from pykardia.include import IncludePlugin
from pykardia.include.yaml import parse
from tests.include.common import IncludeCase


class WriteTests(IncludeCase):
    """Inject failures only at the real rename boundary."""

    async def test_retries_a_rename_that_fails_transiently(self) -> None:
        """Two transient errors yield exactly three replacement attempts and no warnings."""
        attempts = 0

        def rename(source: Path, target: Path) -> None:
            """Fail two native replacements and then perform the actual atomic replacement."""
            nonlocal attempts
            attempts += 1
            if attempts <= 2:
                raise OSError(errno.EBUSY, "resource busy")
            os.replace(source, target)

        include = await self.boot(
            "retry.yml",
            "- id: a\n  name: worker\n  config: 1\n",
            definition=IncludePlugin.with_rename(rename),
        )
        await include.tree().update("a", {"config": 2})
        await include.flush()
        self.assertEqual(attempts, 3)
        self.assertIn("config: 2", include.filename().read_text())
        self.assertFalse(
            [m for m in self.root.log_buffer() if m.level in (Level.Warn, Level.Error)]
        )

    async def test_never_overwrites_a_change_that_lands_while_it_waits_to_retry(self) -> None:
        """The retry rechecks source contents and merges an external change before writing."""
        attempts = 0
        initial = "- id: a\n  name: worker\n  config: 1\n- id: b\n  name: worker\n  config: 1\n"

        def rename(source: Path, target: Path) -> None:
            """Publish a real external file edit immediately before the first transient failure."""
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                target.write_text(initial.rsplit("config: 1", 1)[0] + "config: 2\n")
                raise OSError(errno.EBUSY, "resource busy")
            os.replace(source, target)

        include = await self.boot("race.yml", initial, definition=IncludePlugin.with_rename(rename))
        await include.tree().update("a", {"config": 2})
        await include.flush()
        await self.loader.settled()
        self.assertEqual(attempts, 2)
        self.assertEqual(
            parse(include.filename().read_text())[0],
            [
                {"id": "a", "name": "worker", "config": 2},
                {"id": "b", "name": "worker", "config": 2},
            ],
        )
        self.assertFalse([m for m in self.root.log_buffer() if m.level == Level.Error])

    async def test_does_not_retry_other_errors_but_still_flushes_on_dispose(self) -> None:
        """A permanent failure leaves the running update and journal intact until shutdown."""
        attempts = 0

        def rename(source: Path, target: Path) -> None:
            """Fail once with storage-full, which must not be retried by the same batch."""
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError(errno.ENOSPC, "full")
            os.replace(source, target)

        include = await self.boot(
            "fatal.yml",
            "- id: a\n  name: worker\n  config: 1\n",
            definition=IncludePlugin.with_rename(rename),
        )
        before = include.filename().read_text()
        await include.tree().update("a", {"config": 2})
        await include.flush()
        self.assertEqual(attempts, 1)
        self.assertEqual(include.filename().read_text(), before)
        await self.root.fiber().dispose()
        self.assertEqual(attempts, 2)
        self.assertIn("config: 2", include.filename().read_text())

    async def test_gives_up_after_a_bounded_number_of_attempts_and_keeps_the_change(self) -> None:
        """Ten retries exhaust at eleven attempts; a later dispose flush preserves the edit."""
        attempts = 0
        blocked = True

        def rename(source: Path, target: Path) -> None:
            """Keep the external replacement unavailable until the retry budget is exhausted."""
            nonlocal attempts
            attempts += 1
            if blocked:
                raise OSError(errno.EBUSY, "busy")
            os.replace(source, target)

        include = await self.boot(
            "bounded.yml",
            "- id: a\n  name: worker\n  config: 1\n",
            definition=IncludePlugin.with_rename(rename),
        )
        await include.tree().update("a", {"config": 2})
        await include.flush()
        self.assertEqual(attempts, 11)
        self.assertIn("config: 1", include.filename().read_text())
        self.assertEqual(self.counter.configs[-1], 2)
        self.assertFalse(list(self.base.glob(".*.tmp")))
        blocked = False
        await self.root.fiber().dispose()
        self.assertIn("config: 2", include.filename().read_text())
