"""Source patch.rs integration cases through real loader application."""

from pykardia import JsonValue, Level
from pykardia.include import Include
from tests.include.common import IncludeCase


class PatchIntegrationTests(IncludeCase):
    """Observe plugin configuration, parent groups and warnings after overlaying patches."""

    async def mount(self, patches: list[JsonValue]) -> Include:
        """Read a plain entry and a nested group with the requested real patches."""
        return await self.boot(
            "patch.yml",
            "- id: timer\n  name: worker\n  config: timer\n"
            "- id: group\n  name: group\n  group: true\n  config:\n"
            "  - id: inner\n    name: worker\n    config: inner\n",
            {"patches": patches},
        )

    def warned(self, text: str) -> bool:
        """Search only warning records produced by the actual runtime."""
        return any(log.level is Level.Warn and text in log.text for log in self.root.log_buffer())

    async def test_should_load_without_patches(self) -> None:
        """An empty patch list leaves nested entries active."""
        await self.mount([])
        self.assertCountEqual(self.counter.configs, ["timer", "inner"])

    async def test_should_disable_an_entry_via_patch(self) -> None:
        """A disabled overlay suppresses only its targeted nested entry."""
        await self.mount([{"id": "inner", "disabled": True}])
        self.assertEqual(self.counter.configs, ["timer"])

    async def test_should_override_config_via_patch(self) -> None:
        """A config overlay replaces the entire source configuration."""
        await self.mount([{"id": "inner", "config": {"custom": True}}])
        self.assertCountEqual(self.counter.configs, ["timer", {"custom": True}])

    async def test_should_warn_on_name_mismatch_and_skip_patch(self) -> None:
        """A mismatching name guard warns and leaves the entry running."""
        await self.mount([{"id": "inner", "name": "wrong-name", "disabled": True}])
        self.assertIn("inner", self.counter.configs)
        self.assertTrue(self.warned("name mismatch for inner"))

    async def test_should_warn_on_non_existent_id(self) -> None:
        """A missing target warns without affecting other entries."""
        await self.mount([{"id": "nonexistent", "disabled": True}])
        self.assertIn("inner", self.counter.configs)
        self.assertTrue(self.warned("entry nonexistent not found"))

    async def test_should_insert_entries_into_root_group(self) -> None:
        """Anonymous patch insertions load at the root with an assigned identity."""
        include = await self.mount([{"insert": [{"name": "worker", "config": "extra"}]}])
        self.assertCountEqual(self.counter.configs, ["timer", "inner", "extra"])
        inserted = next(
            e
            for e in include.tree().entries()
            if isinstance(options := e.options(), dict) and options.get("config") == "extra"
        )
        self.assertEqual(inserted.parent(), include.tree().root())

    async def test_should_insert_entries_into_a_specific_group(self) -> None:
        """Targeted patch insertions have the named group's actual parent handle."""
        include = await self.mount(
            [
                {"id": "group", "insert": [{"name": "worker", "config": "extra"}]},
            ]
        )
        self.assertIn("extra", self.counter.configs)
        inserted = next(
            e
            for e in include.tree().entries()
            if isinstance(options := e.options(), dict) and options.get("config") == "extra"
        )
        self.assertEqual(inserted.parent(), include.tree().resolve("group").subgroup())

    async def test_should_warn_when_inserting_into_a_non_group_entry(self) -> None:
        """Insertion into a plain plugin leaves the extra entry absent and warns."""
        await self.mount([{"id": "timer", "insert": [{"name": "worker", "config": "extra"}]}])
        self.assertNotIn("extra", self.counter.configs)
        self.assertIn("inner", self.counter.configs)
        self.assertTrue(self.warned("entry timer is not a group"))

    async def test_should_apply_multiple_patches(self) -> None:
        """A disabling patch and a root insertion both take effect in the same parse."""
        await self.mount(
            [
                {"id": "inner", "disabled": True},
                {"insert": [{"name": "worker", "config": "extra"}]},
            ]
        )
        self.assertCountEqual(self.counter.configs, ["timer", "extra"])

    async def test_should_let_a_later_patch_address_a_row_inserted_by_an_earlier_patch(
        self,
    ) -> None:
        """The second patch resolves the first patch's inserted entry by its new ID."""
        include = await self.mount(
            [
                {"insert": [{"id": "added", "name": "worker", "config": "extra"}]},
                {"id": "added", "disabled": True},
            ]
        )
        self.assertNotIn("extra", self.counter.configs)
        self.assertIn("inner", self.counter.configs)
        self.assertTrue(include.tree().resolve("added").is_disabled())

    async def test_should_validate_name_consistency_matching_name_is_ok(self) -> None:
        """A matching name guard permits the overlay without a mismatch warning."""
        await self.mount([{"id": "inner", "name": "worker", "disabled": True}])
        self.assertNotIn("inner", self.counter.configs)
        self.assertFalse(self.warned("name mismatch"))
