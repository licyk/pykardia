"""Source sync.rs acceptance through real file, journal and loader operations."""

import asyncio

from pykardia import FiberState, JsonValue, Level
from pykardia.include import Include
from pykardia.include.yaml import dump, parse
from pykardia.loader import EntryHandle
from tests.include.common import IncludeCase


def plugin(id: str, value: int, *, anonymous: bool = False) -> dict[str, JsonValue]:
    """Construct source-equivalent counting plugin options."""
    options: dict[str, JsonValue] = {"name": "worker", "config": {"tag": id, "value": value}}
    return options if anonymous else {"id": id, **options}


def group(id: str, config: list[JsonValue]) -> dict[str, JsonValue]:
    """Construct a real loader group containing source entries."""
    return {"id": id, "name": "group", "group": True, "config": config}


class SyncTests(IncludeCase):
    """Check persistence alongside the active plugin state and identity."""

    async def mount(self, entries: list[JsonValue]) -> Include:
        """Mount source-shaped entries as one real include."""
        return await self.boot("sync.yml", dump(entries))

    async def settle(self, include: Include) -> None:
        """Read and flush pending source changes before waiting for actual fibers."""
        await include.refresh()
        await self.loader.settled()

    def read(self, include: Include) -> JsonValue:
        """Parse the resulting physical file for exact structural comparison."""
        return parse(include.filename().read_text())[0]

    def value(self, include: Include, id: str) -> JsonValue:
        """Read config only from an active real plugin fiber."""
        fiber = include.tree().resolve(id).inner_fiber()
        if fiber is None or fiber.state() is not FiberState.Active:
            return None
        config = fiber.config()
        assert isinstance(config, dict)
        return config.get("value")

    def no_errors(self) -> None:
        """Require that no runtime error was hidden by a final-state assertion."""
        self.assertEqual(
            [log.text for log in self.root.log_buffer() if log.level is Level.Error], []
        )

    async def test_coalesces_consecutive_writes_into_the_latest_state(self) -> None:
        """Repeated runtime updates persist the final configuration."""
        include = await self.mount([plugin("a", 1)])
        for n in [2, 3, 4]:
            await include.tree().update("a", {"config": {"tag": "a", "value": n}})
        await self.settle(include)
        self.assertEqual(self.value(include, "a"), 4)
        self.assertEqual(self.read(include), [plugin("a", 4)])
        self.no_errors()

    async def test_keeps_file_and_tree_consistent_when_a_refresh_lands_inside_a_write(self) -> None:
        """Concurrent refresh and update converge to the same file and active configuration."""
        include = await self.mount([plugin("a", 1)])
        await asyncio.gather(
            include.tree().update("a", {"config": {"tag": "a", "value": 2}}), include.refresh()
        )
        await self.settle(include)
        self.assertEqual(self.value(include, "a"), 2)
        self.assertEqual(self.read(include), [plugin("a", 2)])

    async def test_does_not_write_back_when_the_file_removes_or_disables_an_entry(self) -> None:
        """Source removal and disabling preserve comments without self-disposal writes."""
        include = await self.mount([plugin("a", 1), plugin("b", 1)])
        after = "# keep me\n" + dump([plugin("a", 1)])
        include.filename().write_text(after)
        await self.settle(include)
        self.assertEqual(self.counter.disposed, 1)
        self.assertEqual(include.filename().read_text(), after)
        disabled = {**plugin("a", 1), "disabled": True}
        after = "# keep me\n" + dump([disabled])
        include.filename().write_text(after)
        await self.settle(include)
        self.assertIsNone(self.value(include, "a"))
        self.assertEqual(include.filename().read_text(), after)
        self.no_errors()

    async def test_merges_a_file_edit_and_a_runtime_edit_on_different_entries(self) -> None:
        """Independent edits survive each source-tested ordering of refresh and update."""
        for order in ["unwatched", "refresh first", "update first"]:
            with self.subTest(order=order):
                include = await self.mount([plugin("a", 1), plugin("b", 1)])
                include.filename().write_text(dump([plugin("a", 1), plugin("b", 2)]))
                update = include.tree().update("a", {"config": {"tag": "a", "value": 2}})
                if order == "refresh first":
                    await asyncio.gather(include.refresh(), update)
                elif order == "update first":
                    await asyncio.gather(update, include.refresh())
                else:
                    await update
                await self.settle(include)
                self.assertEqual((self.value(include, "a"), self.value(include, "b")), (2, 2))
                self.assertEqual(self.read(include), [plugin("a", 2), plugin("b", 2)])
                self.no_errors()
                self.loader.remove("inc")
                await self.loader.settled()

    async def test_lets_the_file_win_a_conflict_on_the_same_entry_and_reports_it(self) -> None:
        """Conflicting config edits preserve the external value and report the field conflict."""
        include = await self.mount([plugin("a", 1)])
        include.filename().write_text(dump([plugin("a", 3)]))
        await include.tree().update("a", {"config": {"tag": "a", "value": 2}})
        await self.settle(include)
        self.assertEqual(self.value(include, "a"), 3)
        self.assertEqual(self.read(include), [plugin("a", 3)])
        errors = [log.text for log in self.root.log_buffer() if log.level is Level.Error]
        self.assertEqual(len(errors), 1)
        self.assertIn("conflict", errors[0])
        self.assertIn("config", errors[0])

    async def test_combines_a_move_in_the_file_with_an_edit_at_runtime(self) -> None:
        """A source move and runtime config update combine without conflicting."""
        include = await self.mount([plugin("a", 1), group("g", [])])
        include.filename().write_text(dump([group("g", [plugin("a", 1)])]))
        await include.tree().update("a", {"config": {"tag": "a", "value": 2}})
        await self.settle(include)
        self.assertEqual(
            include.tree().resolve("a").parent(), include.tree().resolve("g").subgroup()
        )
        self.assertEqual(self.value(include, "a"), 2)
        self.assertEqual(self.read(include), [group("g", [plugin("a", 2)])])
        self.no_errors()

    async def test_never_writes_over_a_file_it_cannot_parse(self) -> None:
        """Pending edits remain active while a broken document blocks persistence."""
        include = await self.mount([plugin("a", 1)])
        include.filename().write_text("- {\n")
        await include.tree().update("a", {"config": {"tag": "a", "value": 2}})
        await self.settle(include)
        self.assertEqual(self.value(include, "a"), 2)
        self.assertEqual(include.filename().read_text(), "- {\n")
        self.assertTrue(
            any(
                log.level is Level.Warn and "failed to parse" in log.text
                for log in self.root.log_buffer()
            )
        )
        include.filename().write_text(dump([plugin("a", 1), plugin("b", 1)]))
        await self.settle(include)
        self.assertEqual(self.read(include), [plugin("a", 2), plugin("b", 1)])
        self.assertEqual(self.value(include, "b"), 1)

    async def nested(
        self, entries: list[JsonValue], patches: list[JsonValue]
    ) -> tuple[Include, Include]:
        """Mount nested includes with patches declared in the outer source file."""
        (self.base / "inner.yml").write_text(dump(entries))
        outer = await self.mount(
            [
                {
                    "id": "child",
                    "name": "include",
                    "config": {"path": "inner.yml", "patches": patches},
                }
            ]
        )
        tree = outer.tree().resolve("child").subtree()
        assert tree is not None
        inner = Include.of(tree)
        assert inner is not None
        return outer, inner

    async def settle_nested(self, outer: Include, inner: Include) -> None:
        """Flush inner ownership changes before rereading their persisted outer patches."""
        await inner.refresh()
        await self.settle(outer)

    def patches(self, include: Include) -> JsonValue:
        """Extract persisted patch declarations from the physical parent file."""
        entries = self.read(include)
        assert isinstance(entries, list) and isinstance(entries[0], dict)
        config = entries[0]["config"]
        assert isinstance(config, dict)
        return config["patches"]

    async def test_routes_changes_to_patch_owned_keys_into_the_parent_config(self) -> None:
        """Changing a patch-owned disabled flag updates only its parent declaration."""
        outer, inner = await self.nested([plugin("a", 1)], [{"id": "a", "disabled": True}])
        original = inner.filename().read_text()
        self.assertIsNone(self.value(inner, "a"))
        await inner.tree().update("a", {"disabled": False})
        await self.settle_nested(outer, inner)
        self.assertEqual(self.value(inner, "a"), 1)
        self.assertEqual(inner.filename().read_text(), original)
        self.assertEqual(self.patches(outer), [{"id": "a", "disabled": False}])
        self.no_errors()

    async def test_routes_changes_to_inserted_entries_into_their_patch(self) -> None:
        """An inserted entry persists its new config in the insertion array."""
        outer, inner = await self.nested([plugin("a", 1)], [{"insert": [plugin("x", 1)]}])
        original = inner.filename().read_text()
        await inner.tree().update("x", {"config": {"tag": "x", "value": 2}})
        await self.settle_nested(outer, inner)
        self.assertEqual(self.value(inner, "x"), 2)
        self.assertEqual(inner.filename().read_text(), original)
        self.assertEqual(self.patches(outer), [{"insert": [plugin("x", 2)]}])

    async def owner(
        self, patches: list[JsonValue], expected: list[JsonValue], initial: int
    ) -> None:
        """Verify the real effective owner of config and disabled independently."""
        outer, inner = await self.nested([plugin("a", 1)], patches)
        original = inner.filename().read_text()
        self.assertEqual(self.value(inner, "x"), initial)
        await inner.tree().update("x", {"config": {"tag": "x", "value": 3}, "disabled": False})
        await self.settle_nested(outer, inner)
        self.assertEqual(self.value(inner, "x"), 3)
        self.assertEqual(self.patches(outer), expected)
        self.assertEqual(inner.filename().read_text(), original)
        self.no_errors()

    async def test_persists_each_key_to_its_effective_owner_last_override(self) -> None:
        """Config belongs to the last override while a new key belongs to the insertion."""
        await self.owner(
            [{"insert": [plugin("x", 1)]}, {"id": "x", "config": {"tag": "x", "value": 2}}],
            [
                {"insert": [{**plugin("x", 1), "disabled": False}]},
                {"id": "x", "config": {"tag": "x", "value": 3}},
            ],
            2,
        )

    async def test_persists_each_key_to_its_effective_owner_multiple_overrides(self) -> None:
        """Earlier effective overrides retain their old config after the last one changes."""
        await self.owner(
            [
                {"insert": [plugin("x", 1)]},
                {"id": "x", "config": {"tag": "x", "value": 0}},
                {"id": "x", "config": {"tag": "x", "value": 2}},
            ],
            [
                {"insert": [{**plugin("x", 1), "disabled": False}]},
                {"id": "x", "config": {"tag": "x", "value": 0}},
                {"id": "x", "config": {"tag": "x", "value": 3}},
            ],
            2,
        )

    async def test_persists_each_key_to_its_effective_owner_insertion_without_overrides(
        self,
    ) -> None:
        """An unoverridden insertion owns every updated key."""
        await self.owner(
            [{"insert": [plugin("x", 1)]}], [{"insert": [{**plugin("x", 3), "disabled": False}]}], 1
        )

    async def test_persists_each_key_to_its_effective_owner_skipped_override_before_insertion(
        self,
    ) -> None:
        """A patch that preceded its missing target never acquires ownership retroactively."""
        await self.owner(
            [{"id": "x", "config": {"tag": "x", "value": 2}}, {"insert": [plugin("x", 1)]}],
            [
                {"id": "x", "config": {"tag": "x", "value": 2}},
                {"insert": [{**plugin("x", 3), "disabled": False}]},
            ],
            1,
        )

    async def test_persists_each_key_to_its_effective_owner_skipped_name_mismatch(self) -> None:
        """A later name-mismatched override cannot claim a field's persistence target."""
        skipped: JsonValue = {"id": "x", "name": "wrong-plugin", "config": {"tag": "x", "value": 9}}
        await self.owner(
            [
                {"insert": [plugin("x", 1)]},
                {"id": "x", "config": {"tag": "x", "value": 2}},
                skipped,
            ],
            [
                {"insert": [{**plugin("x", 1), "disabled": False}]},
                {"id": "x", "config": {"tag": "x", "value": 3}},
                skipped,
            ],
            2,
        )

    async def test_routes_past_a_name_mismatched_override(self) -> None:
        """A source entry behind a skipped patch still persists directly to its own file."""
        skipped: list[JsonValue] = [
            {"id": "x", "name": "wrong-plugin", "config": {"tag": "x", "value": 2}}
        ]
        outer, inner = await self.nested([plugin("x", 1)], skipped)
        await inner.tree().update("x", {"config": {"tag": "x", "value": 3}})
        await self.settle_nested(outer, inner)
        self.assertEqual(self.value(inner, "x"), 3)
        self.assertEqual(self.patches(outer), skipped)
        self.assertEqual(self.read(inner), [plugin("x", 3)])
        self.no_errors()

    async def replaced_group(self, config: list[JsonValue]) -> tuple[Include, Include]:
        """Create a group whose members are wholly replaced by a parent patch."""
        outer, inner = await self.nested(
            [group("g", [plugin("hidden", 1)])], [{"id": "g", "config": config}]
        )
        self.assertFalse(
            any(isinstance(c, dict) and c.get("tag") == "hidden" for c in self.counter.configs)
        )
        return outer, inner

    async def test_routes_changes_into_the_patch(self) -> None:
        """Editing a replacement group member persists in the patch's config array."""
        outer, inner = await self.replaced_group([plugin("a", 1)])
        original = inner.filename().read_text()
        await inner.tree().update("a", {"config": {"tag": "a", "value": 2}})
        await self.settle_nested(outer, inner)
        self.assertEqual(self.value(inner, "a"), 2)
        self.assertEqual(inner.filename().read_text(), original)
        self.assertEqual(self.patches(outer), [{"id": "g", "config": [plugin("a", 2)]}])

    async def test_places_new_entries_into_the_patch(self) -> None:
        """New members of replaced groups persist alongside their patched siblings."""
        outer, inner = await self.replaced_group([plugin("a", 1)])
        original = inner.filename().read_text()
        id = await self.loader.create(plugin("b", 1, anonymous=True), "inc:child:g")
        await self.settle_nested(outer, inner)
        entry = self.loader.resolve(id)
        options = entry.options()
        assert isinstance(options, dict)
        self.assertEqual(options["config"], {"tag": "b", "value": 1})
        self.assertEqual(inner.filename().read_text(), original)
        self.assertEqual(self.patches(outer), [{"id": "g", "config": [plugin("a", 1), options]}])

    async def test_removes_entries_from_the_patch(self) -> None:
        """Composite removal deletes the replacement member from its parent patch."""
        outer, inner = await self.replaced_group([plugin("a", 1)])
        original = inner.filename().read_text()
        self.loader.remove("inc:child:a")
        await self.settle_nested(outer, inner)
        self.assertEqual(inner.filename().read_text(), original)
        self.assertEqual(self.patches(outer), [{"id": "g", "config": []}])
        self.assertEqual(self.counter.applied, self.counter.disposed)

    def tagged(self, include: Include, tag: str) -> list[EntryHandle]:
        """Find actual source entries by their test plugin's configuration tag."""
        result: list[EntryHandle] = []
        for entry in include.tree().entries():
            options = entry.options()
            if (
                isinstance(options, dict)
                and isinstance(config := options.get("config"), dict)
                and config.get("tag") == tag
            ):
                result.append(entry)
        return result

    async def test_keeps_anonymous_entries_across_file_edits(self) -> None:
        """Anonymous replacement members keep their entry and fiber identity after rereading."""
        outer, inner = await self.replaced_group([plugin("a", 1, anonymous=True)])
        entry = self.tagged(inner, "a")[0]
        fiber = entry.inner_fiber()
        inner.filename().write_text("# touched\n" + inner.filename().read_text())
        await self.settle_nested(outer, inner)
        self.assertEqual(self.tagged(inner, "a"), [entry])
        self.assertIs(entry.inner_fiber(), fiber)

    async def test_routes_an_edit_to_an_entry_whose_group_moved_in_the_same_batch(self) -> None:
        """A concurrent move and child edit persist together after crossing owner boundaries."""
        outer, inner = await self.nested(
            [group("h", [])], [{"insert": [group("g", [plugin("x", 1)])]}]
        )
        await asyncio.gather(
            self.loader.update("inc:child:g", {}, "inc:child:h"),
            inner.tree().update("x", {"config": {"tag": "x", "value": 2}}),
        )
        await inner.flush()
        await self.settle(outer)
        self.assertEqual(self.value(inner, "x"), 2)
        self.assertEqual(self.read(inner), [group("h", [group("g", [plugin("x", 2)])])])
        self.assertEqual(self.patches(outer), [{"insert": []}])
        self.no_errors()

    async def test_keeps_anonymous_entries_stable_across_reloads_without_writing(self) -> None:
        """Source-only changes preserve anonymous identity and postpone ID persistence."""
        anonymous = plugin("anon", 1, anonymous=True)
        include = await self.mount([anonymous, plugin("b", 1)])
        entry = self.tagged(include, "anon")[0]
        fiber = entry.inner_fiber()
        self.assertEqual(include.filename().read_text(), dump([anonymous, plugin("b", 1)]))
        edited = dump([anonymous, plugin("b", 2)])
        include.filename().write_text(edited)
        await self.settle(include)
        self.assertIs(entry.inner_fiber(), fiber)
        self.assertEqual(include.filename().read_text(), edited)
        await include.tree().update("b", {"config": {"tag": "b", "value": 3}})
        await self.settle(include)
        data = self.read(include)
        assert isinstance(data, list) and isinstance(data[0], dict)
        self.assertRegex(str(data[0]["id"]), r"^[0-9a-f]{8}$")
        self.assertEqual(data[0]["config"], {"tag": "anon", "value": 1})

    async def test_does_not_give_a_root_anonymous_plugin_the_nested_plugins_id(self) -> None:
        """Equal anonymous configurations under different parents cannot trade identities."""
        shape = plugin("x", 1, anonymous=True)
        include = await self.mount([{"name": "group", "group": True, "config": [shape]}, shape])
        entries = self.tagged(include, "x")
        root = next(entry for entry in entries if entry.parent() == include.tree().root())
        nested = next(entry for entry in entries if entry.parent() != include.tree().root())
        self.assertNotEqual(root.id(), nested.id())
        fiber = root.inner_fiber()
        applies = self.counter.applied
        include.filename().write_text(dump([shape]))
        await self.settle(include)
        self.assertEqual(self.tagged(include, "x"), [root])
        self.assertIs(root.inner_fiber(), fiber)
        self.assertEqual(self.counter.applied, applies)

    async def test_keeps_an_anonymous_group_and_its_child_stable_when_a_neighbor_changes(
        self,
    ) -> None:
        """A neighboring source edit preserves the identities of an anonymous subtree."""
        anonymous: JsonValue = {
            "name": "group",
            "group": True,
            "config": [plugin("child", 1, anonymous=True)],
        }
        include = await self.mount([anonymous, plugin("b", 1)])
        entry = self.tagged(include, "child")[0]
        parent = entry.parent()
        fiber = entry.inner_fiber()
        owner = parent.owner()
        assert owner is not None
        ids = (owner.id(), entry.id())
        include.filename().write_text(dump([anonymous, plugin("b", 2)]))
        await self.settle(include)
        self.assertEqual(self.tagged(include, "child"), [entry])
        self.assertEqual(entry.parent(), parent)
        self.assertEqual((owner.id(), entry.id()), ids)
        self.assertIs(entry.inner_fiber(), fiber)
        self.assertEqual(self.value(include, "b"), 2)

    async def test_does_not_reuse_an_id_that_the_file_now_writes_explicitly(self) -> None:
        """An explicit source ID takes priority over a previously assigned anonymous ID."""
        anonymous = plugin("anon", 1, anonymous=True)
        include = await self.mount([anonymous])
        old = include.tree().entries()[0].id().split(":")[-1]
        self.assertNotIn(old, include.filename().read_text())
        explicit: JsonValue = {**plugin("other", 1), "id": old}
        include.filename().write_text(dump([anonymous, explicit]))
        await self.settle(include)
        self.assertEqual(include.tree().resolve(old).options(), explicit)
        self.assertNotEqual(self.tagged(include, "anon")[0].id(), f"inc:{old}")

    async def test_does_not_reuse_an_id_owned_by_an_explicit_patch_insert(self) -> None:
        """Explicit insertions reserve IDs when a later reread assigns anonymous source IDs."""
        anonymous = plugin("anon", 1, anonymous=True)
        include = await self.mount([anonymous])
        old = include.tree().entries()[0].id().split(":")[-1]
        explicit: JsonValue = {**plugin("other", 9), "id": old}
        await self.loader.update(
            "inc",
            {"config": {"path": str(include.filename()), "patches": [{"insert": [explicit]}]}},
        )
        await self.loader.settled()
        include.filename().write_text("# reread\n" + dump([anonymous]))
        await self.settle(include)
        self.assertEqual(include.tree().resolve(old).options(), explicit)
        self.assertNotEqual(self.tagged(include, "anon")[0].id(), f"inc:{old}")

    async def test_keeps_runtime_changes_in_memory_when_the_file_is_read_only(self) -> None:
        """A read-only source retains runtime overlays even when unrelated file data changes."""
        include = await self.mount([plugin("a", 1), plugin("b", 1)])
        original = include.filename().read_text()
        include.filename().chmod(0o444)
        await include.tree().update("a", {"config": {"tag": "a", "value": 2}})
        await self.settle(include)
        self.assertEqual(self.value(include, "a"), 2)
        self.assertEqual(include.filename().read_text(), original)
        self.assertTrue(
            any(
                log.level is Level.Warn and "read-only" in log.text
                for log in self.root.log_buffer()
            )
        )
        include.filename().chmod(0o644)
        include.filename().write_text(dump([plugin("a", 1), plugin("b", 2)]))
        include.filename().chmod(0o444)
        await self.settle(include)
        self.assertEqual((self.value(include, "a"), self.value(include, "b")), (2, 2))
        self.assertEqual(self.read(include), [plugin("a", 1), plugin("b", 2)])
        include.filename().chmod(0o644)

    async def test_removes_a_nested_entry_through_its_composite_id(self) -> None:
        """Public composite IDs remove the local entry and persist only its remaining sibling."""
        include = await self.mount([plugin("a", 1), plugin("b", 1)])
        self.loader.remove("inc:a")
        await self.settle(include)
        with self.assertRaises(ValueError):
            include.tree().resolve("a")
        self.assertEqual(self.value(include, "b"), 1)
        self.assertEqual(self.read(include), [plugin("b", 1)])

    async def test_removes_an_entry_of_a_group_through_its_composite_id(self) -> None:
        """Composite IDs address grouped entries without adding group names to the ID."""
        include = await self.mount([group("g", [plugin("a", 1)]), plugin("b", 1)])
        self.loader.remove("inc:a")
        await self.settle(include)
        self.assertEqual(self.read(include), [group("g", []), plugin("b", 1)])
        self.assertEqual(self.counter.disposed, 1)

    async def test_retries_a_write_that_failed_earlier_when_disposing(self) -> None:
        """Disposal retries a pending read-only write after permissions recover."""
        include = await self.mount([plugin("a", 1)])
        original = include.filename().read_text()
        include.filename().chmod(0o444)
        await include.tree().update("a", {"config": {"tag": "a", "value": 2}})
        await self.settle(include)
        self.assertEqual(include.filename().read_text(), original)
        include.filename().chmod(0o644)
        await self.root.fiber().dispose()
        self.assertEqual(self.read(include), [plugin("a", 2)])

    async def test_flushes_pending_changes_on_dispose(self) -> None:
        """An update queued with shutdown is flushed before include cleanup completes."""
        include = await self.mount([plugin("a", 1)])
        await asyncio.gather(
            self.loader.update("inc:a", {"config": {"tag": "a", "value": 2}}),
            self.root.fiber().dispose(),
        )
        self.assertEqual(self.read(include), [plugin("a", 2)])

    async def test_plugin_self_updates_and_self_disposals_are_written_back(self) -> None:
        """Direct fiber changes persist new config and a self-disposed plugin's disabled flag."""
        include = await self.mount([plugin("a", 1), plugin("b", 1)])
        a, b = (include.tree().resolve(id).inner_fiber() for id in ["a", "b"])
        assert a is not None and b is not None
        await a.update({"tag": "a", "value": 7})
        await b.dispose()
        await self.settle(include)
        self.assertEqual(self.read(include), [plugin("a", 7), {**plugin("b", 1), "disabled": True}])

    async def test_moves_an_entry_into_another_include_as_a_single_instance(self) -> None:
        """Cross-include movement persists both files and never leaves two active instances."""
        source = await self.mount([plugin("m", 1)])
        target_path = self.base / "target.yml"
        target_path.write_text(dump([group("g", [])]))
        await self.loader.create(
            {"id": "target", "name": "include", "config": {"path": str(target_path)}}
        )
        await self.loader.settled()
        tree = self.loader.resolve("target").subtree()
        assert tree is not None
        target = Include.of(tree)
        assert target is not None
        await self.loader.update("inc:m", {}, "target:g")
        await self.settle(source)
        await self.settle(target)
        self.assertEqual(self.read(source), [])
        self.assertEqual(self.read(target), [group("g", [plugin("m", 1)])])
        self.assertEqual(self.counter.applied - self.counter.disposed, 1)
        self.assertEqual(self.value(target, "m"), 1)
        with self.assertRaises(ValueError):
            self.loader.resolve("inc:m")
        self.loader.remove("target:m")
        await target.flush()
        await self.loader.settled()
        self.assertEqual(self.counter.applied, self.counter.disposed)
        self.assertEqual(self.read(target), [group("g", [])])

    async def test_accepts_a_drive_letter_path(self) -> None:
        """The source path.rs case accepts an absolute filesystem path on this platform."""
        include = await self.mount([plugin("inner", 1)])
        self.assertTrue(include.filename().is_absolute())
        self.assertEqual(include.filename(), self.base / "sync.yml")
        self.assertEqual(self.value(include, "inner"), 1)
