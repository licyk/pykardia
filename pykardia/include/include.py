"""Serialized file/tree reconciliation with journal recovery and atomic writeback."""

from __future__ import annotations

import asyncio
import errno
import os
import threading
import weakref
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path

from pykardia.core import Context, Disposer, Inject, JsonValue, Plugin, PluginDef, decode
from pykardia.core.config import PathSegment
from pykardia.core.runtime import Completion
from pykardia.core.value import json_equal
from pykardia.fs import Fs, FsExt
from pykardia.include import yaml
from pykardia.include.document import (
    ConfigFileError,
    Stage,
    json_syntax,
    parse_document,
    pretty_json,
)
from pykardia.include.identity import Anonymous, assign, collect_ids, entry_path
from pykardia.include.journal import Journal, apply_journal, equal, merge, reconcile, record
from pykardia.include.patch import (
    FILE,
    EmbeddedOwner,
    PatchIndex,
    PatchOwner,
    apply_patches,
    ensure_ids,
    ensure_insert_ids,
    route_journal,
)
from pykardia.include.tree import find, flatten, id_of
from pykardia.include.yaml import Syntax
from pykardia.loader import (
    EXPR_KEY,
    MISSING,
    EntryChange,
    Loader,
    Location,
    TreeBackend,
    TreeHandle,
    expr_source,
    value_at,
)
from pykardia.loader.backend import Missing

Rename = Callable[[Path, Path], object]


@dataclass
class IncludeConfig:
    """A config file, optional initial contents, patch overlays and watch mode."""

    path: str
    initial: list[JsonValue] | None = None
    patches: list[JsonValue] = field(default_factory=list)
    watch: bool = False


class IncludePlugin(Plugin[IncludeConfig]):
    """Mount a file-backed loader subtree with two-way source synchronization."""

    NAME = "include"
    Config = IncludeConfig

    @staticmethod
    def inject() -> Inject:
        """Require the loader; watch mode creates its own fs-dependent child."""
        return Inject().with_(Loader.NAME)

    @staticmethod
    async def apply(ctx: Context, config: IncludeConfig) -> None:
        """Start an include using the operating system's atomic file replacement."""
        await Include.run(ctx, deepcopy(config), os.replace)

    @staticmethod
    def with_rename(rename: Rename) -> PluginDef:
        """Use an explicit replacement boundary for filesystem fault injection."""

        async def apply(ctx: Context, config: IncludeConfig) -> None:
            """Retain the supplied rename operation on this actual include instance."""
            await Include.run(ctx, deepcopy(config), rename)

        return (
            PluginDef.typed(apply, lambda value: decode(value, IncludeConfig))
            .name("include")
            .inject(["loader"])
        )


class StaleWrite(Exception):
    """The file no longer has the content against which a write was prepared."""

    def __init__(self) -> None:
        """Use the source's stable stale-write diagnostic."""
        super().__init__("the config file changed while it was being written")


@dataclass
class Snapshot:
    """Last read/written file content, assigned source entries and their syntax positions."""

    content: str
    data: list[JsonValue]
    syntax: Syntax


@dataclass
class State:
    """Pending source changes, ownership and shared read/write/apply work."""

    patches: list[JsonValue]
    cache: Snapshot | None = None
    journal: Journal = field(default_factory=dict)
    writing: Journal = field(default_factory=dict)
    index: PatchIndex = field(default_factory=PatchIndex)
    anonymous: list[Anonymous] = field(default_factory=list)
    dirty_read: bool = False
    dirty_write: bool = False
    draining: Completion[None] | None = None
    pending: list[JsonValue] | None = None
    applying: Completion[None] | None = None
    disposed: bool = False


class Include:
    """One running include whose runtime edits are persisted to their original owners."""

    def __init__(
        self, ctx: Context, loader: Loader, filename: Path, config: IncludeConfig, rename: Rename
    ) -> None:
        """Create state before mounting the weakly referenced backend."""
        self._ctx, self._loader, self._filename = ctx, loader, filename
        self._watch, self._rename = config.watch, rename
        patches = deepcopy(config.patches)
        ensure_insert_ids(patches, lambda id: False)
        self._state = State(patches)
        self._lock = threading.RLock()
        self._seq = 0
        self._tree = loader.create_tree(ctx, Backend(self))

    @staticmethod
    async def run(ctx: Context, config: IncludeConfig, rename: Rename) -> None:
        """Mount, configure updates/watch, read initial data and retain flush-before-stop."""
        loader = ctx.get(Loader).arc()
        entry = loader.entry_of(ctx)
        base = entry.parent().tree().base_dir() if entry is not None else Path()
        filename = base / config.path
        if not filename.is_absolute():
            filename = Path.cwd() / filename
        if filename.suffix not in (".json", ".yaml", ".yml"):
            raise ValueError(f'extension "{filename.suffix[1:]}" not supported')
        include = Include(ctx, loader, filename, config, rename)

        def update(next_: IncludeConfig) -> Awaitable[None] | None:
            """Apply patches in place; path or watch changes require a fresh episode."""
            if next_.path != config.path or next_.watch != include._watch:
                return None
            patches = deepcopy(next_.patches)
            ensure_insert_ids(patches, lambda id: False)
            with include._lock:
                include._state.patches = patches
                include.schedule_apply()
                return include._state.applying or Completion.ready(None)

        ctx.on_update_with(IncludeConfig, update)
        if config.watch:

            async def watch(context: Context) -> None:
                """Own the native watch on a child that waits for the fs service."""
                watcher = FsExt(context.get(Fs)).watch(filename)

                def changed(path: Path) -> None:
                    """Queue refresh without making the native callback wait for persistence."""
                    ctx._root.driver.spawn(include.refresh())

                watcher.on_change(changed)

            ctx.inject(["fs"], watch)
        try:
            await include.read()
        except ConfigFileError as error:
            if not error.not_found:
                raise
            if config.initial is None:
                raise ValueError(f"config file not found: {filename}") from error
            initial = deepcopy(config.initial)
            ensure_ids(initial, lambda id: False)
            await include.write_text(include.dump(initial), None)
            await include.read()
        ctx.defer("include", Disposer.future(include.stop))
        with include._lock:
            applying = include._state.applying
        if applying is not None:
            await applying

    @staticmethod
    def of(tree: TreeHandle) -> Include | None:
        """Retrieve the include owning a tree, if its backend represents an include."""
        owner = tree.handle()
        return owner if isinstance(owner, Include) else None

    def filename(self) -> Path:
        """Return the absolute lexical path used to access this file."""
        return self._filename

    def tree(self) -> TreeHandle:
        """Return the mounted loader subtree."""
        return self._tree

    async def refresh(self) -> None:
        """Request a read and wait for all resulting reads, writes and tree application."""
        with self._lock:
            self._state.dirty_read = True
        self.drain()
        await self.flush()

    async def flush(self) -> None:
        """Observe shared work until no read, write or tree application remains."""
        while True:
            with self._lock:
                draining, applying = self._state.draining, self._state.applying
            if draining is None and applying is None:
                return
            if draining is not None:
                await draining
            if applying is not None:
                await applying

    async def stop(self) -> None:
        """Flush user mutations before internally removing entries without disabling them."""
        with self._lock:
            state = self._state
            state.disposed, state.pending = True, None
            if state.journal:
                state.dirty_write = True
        self.drain()
        await self.flush()
        self._tree.root().stop()

    def commit(self, change: EntryChange) -> None:
        """Record one synchronous loader mutation and schedule source writeback."""
        owner = change.group.owner() if change.group != self._tree.root() else None
        parent = id_of(owner.options()) if owner is not None else None
        position = next(
            (i for i, options in enumerate(change.group.data()) if id_of(options) == change.id), 0
        )
        with self._lock:
            record(
                self._state.journal,
                change.id,
                change.options,
                change.legacy,
                change.from_ is not None,
                parent,
                position,
            )
            self._state.dirty_write = True
        self.drain()

    def drain(self) -> None:
        """Start at most one owned read/write loop without transferring observer cancellation."""
        with self._lock:
            if self._state.draining is None:
                self._state.draining = self._ctx._root.driver.spawn(self.drain_loop())

    async def drain_loop(self) -> None:
        """Read before writing; pause writes when the file cannot be parsed safely."""
        while True:
            with self._lock:
                state = self._state
                if not state.dirty_read and not state.dirty_write:
                    state.draining = None
                    return
                read, write = state.dirty_read, state.dirty_write
                state.dirty_read = False
            if read:
                try:
                    await self.read()
                except ConfigFileError as error:
                    self._ctx.logger().warn(error)
                    with self._lock:
                        if not self._state.dirty_read:
                            self._state.draining = None
                            return
                    continue
            if write:
                with self._lock:
                    self._state.dirty_write = False
                try:
                    await self.write()
                except Exception as error:
                    self._ctx.logger().log_error(error)

    async def read(self) -> None:
        """Read source bytes, assign stable IDs and reconcile file-owned journal conflicts."""
        try:
            content = self._filename.read_bytes().decode("utf-8")
        except (OSError, UnicodeError) as error:
            raise ConfigFileError(
                Stage.Read,
                self._filename,
                str(error),
                not_found=isinstance(error, FileNotFoundError),
            ) from error
        with self._lock:
            if self._state.cache is not None and self._state.cache.content == content:
                return
        data, syntax = parse_document(content, self._filename)
        with self._lock:
            state = self._state
            self.assign_ids(data)
            if state.cache is not None:
                index = apply_patches(data, state.patches, lambda message: None).index
                conflicts = reconcile(
                    state.journal, flatten(state.cache.data), flatten(data), index.file_owned
                )
                for conflict in conflicts:
                    self._ctx.logger().error(
                        f"config conflict in {self._filename}: entry {conflict.id} "
                        f"{conflict.reason}; file wins"
                    )
            state.cache = Snapshot(content, data, syntax)
            self.schedule_apply()
            if state.journal:
                state.dirty_write = True

    async def write(self) -> None:
        """Prepare a batch against the cached file and atomically publish its routed changes."""
        with self._lock:
            state = self._state
            if state.cache is None or not state.journal:
                return
            expected = state.cache.content
        try:
            current = self._filename.read_bytes().decode("utf-8")
        except FileNotFoundError:
            with self._lock:
                pending = len(self._state.journal)
            self._ctx.logger().warn(
                f"config file {self._filename} is missing, "
                f"{pending} pending change(s) kept in memory"
            )
            return
        if current != expected:
            with self._lock:
                self._state.dirty_read = self._state.dirty_write = True
            return
        with self._lock:
            state = self._state
            state.writing, state.journal = state.journal, {}
            assert state.cache is not None
            batch, data, patches = (
                deepcopy(state.writing),
                deepcopy(state.cache.data),
                deepcopy(state.patches),
            )
        patched = patches if route_journal(batch, data, patches, self.warn) else None
        with self._lock:
            cache = self._state.cache
            unchanged = cache is not None and json_equal(cache.data, data)
        if unchanged:
            self.commit_batch(None, patched)
            return
        text = self.dump(data)
        try:
            read_only = self._filename.stat().st_mode & 0o222 == 0
        except OSError:
            read_only = False
        if read_only:
            pending = self.restore()
            self._ctx.logger().warn(
                f"config file {self._filename} is read-only, "
                f"{pending} pending change(s) kept in memory"
            )
            return
        try:
            await self.write_text(text, expected)
        except Exception as error:
            pending = self.restore()
            if isinstance(error, StaleWrite):
                with self._lock:
                    self._state.dirty_read = self._state.dirty_write = True
                return
            self._ctx.logger().warn(
                f"failed to write config file {self._filename}, "
                f"{pending} pending change(s) kept in memory"
            )
            raise
        self.commit_batch(Snapshot(text, data, json_syntax(text)), patched)

    def commit_batch(self, cache: Snapshot | None, patches: list[JsonValue] | None) -> None:
        """Commit written source and routed patches together before retiring their batch."""
        with self._lock:
            if cache is not None:
                self._state.cache = cache
            if patches is not None:
                self._state.patches = deepcopy(patches)
            self._state.writing.clear()
            self.schedule_apply()
        if patches is not None:
            self.persist_patches(patches)

    def restore(self) -> int:
        """Restore a failed batch ahead of newer reports, folding overlapping entry changes."""
        with self._lock:
            state = self._state
            batch, state.writing = state.writing, {}
            for id, change in state.journal.items():
                merge(batch, id, change)
            state.journal = batch
            return len(batch)

    def persist_patches(self, patches: list[JsonValue]) -> None:
        """Write patched fields through the owning include entry without blocking this drain."""
        config = self._ctx.fiber().config()
        if isinstance(config, dict):
            config["patches"] = deepcopy(patches)
        if self._loader.entry_of(self._ctx) is None:
            self.warn(f"patches of {self._filename} changed at runtime but cannot be persisted")
            return
        try:
            settle = self._ctx.fiber().update(config)
        except Exception as error:
            self._ctx.logger().log_error(error)
            return

        async def observe() -> None:
            """Report update failures without joining the tree reconciliation being driven."""
            try:
                await settle
            except Exception as error:
                self._ctx.logger().log_error(error)

        self._ctx._root.driver.spawn(observe())

    async def write_text(self, text: str, expected: str | None) -> None:
        """Write a same-directory temporary file and retry only transient replacement errors."""
        with self._lock:
            sequence = self._seq
            self._seq += 1
        temporary = self._filename.with_name(f".{self._filename.name}.{os.getpid()}.{sequence}.tmp")
        try:
            temporary.write_bytes(text.encode("utf-8"))
            for attempt in range(11):
                if expected is not None:
                    try:
                        current = self._filename.read_bytes().decode("utf-8")
                    except FileNotFoundError:
                        current = None
                    if current != expected:
                        raise StaleWrite()
                try:
                    self._rename(temporary, self._filename)
                    return
                except OSError as error:
                    if attempt >= 10 or error.errno not in (errno.EACCES, errno.EPERM, errno.EBUSY):
                        raise
                await asyncio.sleep(0.02 * (attempt + 1))
        except BaseException:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def dump(self, data: list[JsonValue]) -> str:
        """Serialize using the extension-selected source format."""
        return pretty_json(data) if self._filename.suffix == ".json" else yaml.dump(data)

    def schedule_apply(self) -> None:
        """Replace the intended tree snapshot while one shared apply loop drains it."""
        state = self._state
        if state.disposed or state.cache is None:
            return
        patched = apply_patches(state.cache.data, state.patches, self.warn)
        data = apply_journal(patched.data, state.writing, lambda id, key: True, self.warn)
        state.pending = apply_journal(data, state.journal, lambda id, key: True, self.warn)
        state.index = patched.index
        if state.applying is None:
            state.applying = self._ctx._root.driver.spawn(self.apply_loop())

    async def apply_loop(self) -> None:
        """Apply the latest pending snapshot and then check for updates received meanwhile."""
        while True:
            with self._lock:
                tree, self._state.pending = self._state.pending, None
                if tree is None:
                    self._state.applying = None
                    return
            await self._tree.root().update(tree)

    def assign_ids(self, data: list[JsonValue]) -> None:
        """Reuse previous anonymous IDs unless the current source or inserts claim them."""
        state = self._state
        used: set[str] = set()
        collect_ids(data, used)
        for patch in state.patches:
            insert = patch.get("insert") if isinstance(patch, dict) else None
            if isinstance(insert, list):
                collect_ids(insert, used)
        previous, state.anonymous = state.anonymous, []
        store = {id for entry in self._tree.entries() if (id := id_of(entry.options())) is not None}
        assign(data, None, used, previous, state.anonymous, store)

    def locate(
        self,
        id: str,
        path: list[PathSegment],
        value: JsonValue | Missing = MISSING,
        offset: int | None = None,
    ) -> Location | None:
        """Locate only matching written values, following patch ownership into parent files."""
        with self._lock:
            state = self._state
            cache = state.cache
            if cache is None:
                return None
            owner = (
                state.index.key(id, path[0])
                if path and isinstance(path[0], str)
                else state.index.entry(id)
            )
            if owner == FILE:
                indices = find(cache.data, id)
                if indices is None:
                    return None
                full = [*entry_path(indices), *path]
                written = value_at(cache.data, full)
                if not isinstance(value, Missing) and not equal(written, value):
                    return None
                position = (
                    cache.syntax.locate_expr(full, offset)
                    if offset is not None
                    else cache.syntax.locate(full)
                )
                return (
                    Location(self._filename, position.line, position.column)
                    if position is not None
                    else None
                )
            if isinstance(owner, PatchOwner):
                rest = path
            else:
                assert isinstance(owner, EmbeddedOwner)
                patch = state.patches[owner.index]
                members = patch.get(owner.key) if isinstance(patch, dict) else None
                indices = find(members, id) if isinstance(members, list) else None
                if indices is None:
                    return None
                rest = [owner.key, *entry_path(indices), *path]
            parent_path: list[PathSegment] = ["config", "patches", owner.index, *rest]
        entry = self._loader.entry_of(self._ctx)
        own = id_of(entry.options()) if entry is not None else None
        if entry is None or own is None:
            return None
        tree = entry.parent().tree()
        if offset is not None:
            expression = expr_source(value) if not isinstance(value, Missing) else None
            return (
                tree.locate_expr(own, parent_path, expression, offset)
                if expression is not None
                else None
            )
        return tree.locate(own, parent_path, value)

    def warn(self, message: str) -> None:
        """Send include ownership warnings through the root loader logger."""
        self._ctx.root().logger_named("loader").warn(message)


class Backend(TreeBackend):
    """Delegate loader persistence and location hooks without retaining an include cycle."""

    def __init__(self, include: Include) -> None:
        """Keep a weak owner, as the source tree backend does."""
        self.owner = weakref.ref(include)

    def commit(self, change: EntryChange) -> None:
        """Forward mutations while the include owner is live."""
        owner = self.owner()
        if owner is not None:
            owner.commit(change)

    def base_dir(self) -> Path | None:
        """Resolve nested includes relative to their containing file's lexical directory."""
        owner = self.owner()
        return owner.filename().parent if owner is not None else None

    def handle(self) -> object | None:
        """Expose the actual owning include through TreeHandle.handle."""
        return self.owner()

    def locate(
        self, id: str, path: list[PathSegment], value: JsonValue | Missing = MISSING
    ) -> Location | None:
        """Forward source-value matching and location lookup."""
        owner = self.owner()
        return owner.locate(id, path, value) if owner is not None else None

    def locate_expr(
        self, id: str, path: list[PathSegment], expr: str, offset: int
    ) -> Location | None:
        """Locate a matching expression object in the file or its declaring patch."""
        owner = self.owner()
        return owner.locate(id, path, {EXPR_KEY: expr}, offset) if owner is not None else None
