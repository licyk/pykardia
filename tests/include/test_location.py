"""Source location.rs acceptance for source frames, validation and expression offsets."""

import json

from pykardia import Issue, JsonValue, Level, ValidationError
from pykardia.include import Include
from tests.include.common import IncludeCase


def validate_schema(value: JsonValue) -> JsonValue:
    """Apply the source fixture's numeric port and string hosts schema."""
    if value is None:
        raise ValidationError.message("config is required")
    obj = value if isinstance(value, dict) else {}
    issues: list[Issue] = []
    if type(obj.get("port")) not in (int, float):
        issues.append(Issue("port must be a number", ["port"]))
    hosts = obj.get("hosts")
    if isinstance(hosts, list):
        for index, host in enumerate(hosts):
            if not isinstance(host, str):
                issues.append(Issue("host must be a string", ["hosts", index]))
                break
    if issues:
        raise ValidationError(issues)
    return value


class LocationTests(IncludeCase):
    """Check exact source positions against the original integration expectations."""

    async def start(self, source: str, name: str = "location.yml") -> Include:
        """Register the source schema fixture and mount its real source document."""
        self.source = source
        self.path = self.base / name
        self.catalog.register_def(
            "schema-plugin", self.counter.definition().validate_with(validate_schema)
        )
        self.catalog.register_def("config-plugin", self.counter.definition())
        return await self.boot(name, source)

    def at(self, needle: str) -> str:
        """Locate a source spelling independently of the production syntax tree."""
        before = self.source[: self.source.index(needle)]
        return f"{self.path}:{before.count(chr(10)) + 1}:{len(before.rsplit(chr(10), 1)[-1]) + 1}"

    def logs(self, level: Level = Level.Error) -> list[str]:
        """Read actual runtime diagnostics at the requested severity."""
        return [log.text for log in self.root.log_buffer() if log.level is level]

    def error(self, id: str = "a") -> str:
        """Require a diagnostic containing the requested source entry frame."""
        return next(error for error in self.logs() if f"at {id} (" in error)

    async def refresh(self, include: Include, source: str) -> None:
        """Replace source text and settle the real include and loader."""
        self.source = source
        self.path.write_text(source)
        await include.refresh()
        await self.loader.settled()

    async def test_points_a_validation_issue_at_the_value_in_the_file(self) -> None:
        """The invalid scalar and entry frame retain their exact file coordinates."""
        await self.start('- id: a\n  name: schema-plugin\n  config:\n    port: "80"\n')
        position = self.at('"80"')
        self.assertEqual(
            self.logs(),
            [
                "invalid config:\n"
                f"  - port must be a number (at port, {position})\n    at a ({self.path}:1:3)",
            ],
        )

    async def test_points_a_missing_value_at_the_key_that_should_hold_it(self) -> None:
        """Absent paths fall back to their containing key and absent config to the entry."""
        await self.start(
            "- id: a\n  name: schema-plugin\n  config:\n    hosts: [localhost, 1]\n"
            "- id: b\n  name: schema-plugin\n"
        )
        self.assertIn(
            f"  - port must be a number (at port, {self.at('config:')})\n"
            f"  - host must be a string (at hosts.1, {self.at('1]')})",
            self.error(),
        )
        self.assertIn(f"  - config is required (at {self.at('id: b')})", self.error("b"))

    async def test_locates_entries_nested_in_groups(self) -> None:
        """Validation of evaluated data points at the expression and includes group frames."""
        await self.start(
            "- id: g\n  name: group\n  group: true\n  config:\n"
            "    - id: a\n      name: schema-plugin\n      config:\n        port: !!js \"'80'\"\n"
        )
        position = self.at("\"'80'\"")
        self.assertIn(f"(at port, {position})", self.error())
        self.assertIn(f"    at a ({self.path}:5:7)\n    at g ({self.path}:1:3)", self.error())

    async def test_locates_patched_values_in_the_file_that_declares_the_patch(self) -> None:
        """Override and insertion diagnostics both trace into the parent patch source."""
        (self.base / "inner.yml").write_text(
            "- id: a\n  name: schema-plugin\n  config: {port: 80}\n"
        )
        await self.start(
            "- id: child\n  name: include\n  config:\n    path: inner.yml\n"
            '    patches:\n      - id: a\n        config: { port: "80" }\n'
            "      - insert:\n          - id: b\n            name: schema-plugin\n"
            "            config: { port: 80, hosts: [1] }\n"
        )
        position = self.at('"80"')
        self.assertIn(f"(at port, {position})", self.error())
        self.assertIn(f"(at hosts.0, {self.at('1]')})", self.error("b"))

    async def test_locates_overrides_of_inserted_entries_in_the_override(self) -> None:
        """A later overriding field owns its position instead of the initial insertion."""
        (self.base / "inner.yml").write_text("[]\n")
        await self.start(
            "- id: child\n  name: include\n  config:\n    path: inner.yml\n"
            "    patches:\n      - insert:\n          - id: a\n            name: schema-plugin\n"
            '            config: { port: 80 }\n      - id: a\n        config: { port: "80" }\n'
        )
        position = self.at('"80"')
        self.assertIn(f"(at port, {position})", self.error())

    async def test_aliases_locate_a_config_value_at_its_anchor(self) -> None:
        """Both scalar and mapping aliases use the anchored scalar's original position."""
        await self.start(
            '- id: a\n  name: schema-plugin\n  config: &c\n    port: &p "80"\n'
            "- id: b\n  name: schema-plugin\n  config: *c\n"
            "- id: c\n  name: schema-plugin\n  config:\n    port: *p\n"
        )
        self.assertEqual(len(self.logs()), 3)
        position = self.at('"80"')
        for id in ["a", "b", "c"]:
            self.assertIn(f"(at port, {position})", self.error(id))

    async def test_aliases_point_a_missing_value_under_an_alias_at_the_key_that_holds_it(
        self,
    ) -> None:
        """Missing values in aliased collections fall back to the alias-bearing key."""
        await self.start(
            "- id: a\n  name: config-plugin\n  intercept: &c {}\n"
            "- id: b\n  name: schema-plugin\n  config: *c\n"
        )
        self.assertIn(f"(at port, {self.at('config: *c')})", self.error("b"))

    async def test_aliases_locate_an_entry_option_at_its_anchor(self) -> None:
        """Aliased entry fields point at the anchored scalar."""
        await self.start(
            "- id: a\n  name: config-plugin\n  isolate: { x: &g 1 }\n"
            "- id: b\n  name: config-plugin\n  group: *g\n"
        )
        self.assertIn(f"(at group, {self.at('1 }')})", self.error("b"))

    async def test_aliases_locate_an_expression_at_its_anchor(self) -> None:
        """Expression failures inside an alias retain the anchored expression offset."""
        await self.start(
            '- id: a\n  name: config-plugin\n  intercept: { x: &x !!js "1 + foo" }\n'
            "- id: b\n  name: config-plugin\n  disabled: *x\n"
        )
        self.assertIn(
            f"    at disabled ({self.at('foo')})\n    at b ({self.path}:4:3)", self.error("b")
        )

    async def test_locates_entries_of_a_group_config_replaced_by_a_patch(self) -> None:
        """Replacement members belong to the patch while the group's frame remains in its file."""
        inner = self.base / "inner.yml"
        inner.write_text("- id: g\n  name: group\n  group: true\n  config: []\n")
        await self.start(
            "- id: child\n  name: include\n  config:\n    path: inner.yml\n"
            "    patches:\n      - id: g\n        config:\n          - id: a\n"
            '            name: schema-plugin\n            config: { port: "80" }\n'
        )
        position = self.at('"80"')
        self.assertIn(f"(at port, {position})", self.error())
        self.assertIn(f"    at a ({self.path}:8:13)\n    at g ({inner}:1:3)", self.error())

    async def test_ignores_patches_that_do_not_apply(self) -> None:
        """A skipped name guard cannot replace the field's original source location."""
        inner = self.base / "inner.yml"
        inner.write_text('- id: a\n  name: schema-plugin\n  config: { port: "80" }\n')
        await self.start(
            "- id: child\n  name: include\n  config:\n    path: inner.yml\n"
            "    patches:\n      - id: a\n        name: other-plugin\n"
            "        config: { port: 80 }\n"
        )
        self.assertIn(f"(at port, {inner}:3:19)", self.error())

    async def test_follows_patches_across_nested_includes(self) -> None:
        """An outer patch can own an inner patch's value while entry frames span three files."""
        inner = self.base / "inner.yml"
        inner.write_text("- id: a\n  name: schema-plugin\n  config: {port: 80}\n")
        middle = self.base / "middle.yml"
        middle.write_text(
            "- id: child\n  name: include\n  config:\n    path: inner.yml\n"
            '    patches:\n      - id: a\n        config: { port: "middle" }\n'
        )
        await self.start(
            "- id: mid\n  name: include\n  config:\n    path: middle.yml\n"
            "    patches:\n      - id: child\n        config:\n          path: inner.yml\n"
            '          patches:\n            - id: a\n              config: { port: "outer" }\n'
        )
        position = self.at('"outer"')
        self.assertIn(f"(at port, {position})", self.error())
        self.assertIn(
            f"    at a ({inner}:1:3)\n    at child ({middle}:1:3)\n    at mid ({self.path}:1:3)",
            self.error(),
        )

    async def test_leaves_a_config_passed_at_runtime_unlocated(self) -> None:
        """Direct invalid config updates have no fabricated file coordinates."""
        include = await self.start("- id: a\n  name: schema-plugin\n  config: {port: 80}\n")
        fiber = include.tree().resolve("a").inner_fiber()
        assert fiber is not None
        with self.assertRaises(Exception) as caught:
            fiber.update({"port": "runtime"})
        self.assertEqual(
            str(caught.exception), "invalid config:\n  - port must be a number (at port)"
        )

    async def test_leaves_entries_outside_any_file_untraced(self) -> None:
        """Entries created on the root loader have validation paths but no include frames."""
        await self.start("[]\n")
        await self.loader.create({"name": "schema-plugin", "config": {"port": "80"}})
        await self.loader.settled()
        self.assertEqual(self.logs(), ["invalid config:\n  - port must be a number (at port)"])

    async def test_entry_fields_point_invalid_fields_at_their_values(self) -> None:
        """Entry validation reports all offending values in source order."""
        include = await self.start(
            '- id: a\n  name: config-plugin\n  disabled: "no"\n'
            "  inject: [loader, 1]\n- id: b\n  group: true\n"
        )
        position = self.at('"no"')
        self.assertEqual(
            self.error(),
            f"invalid entry options:\n  - disabled must be a boolean (at disabled, {position})\n"
            f"  - inject.1 must be a string (at inject.1, {self.at('1]')})\n"
            f"    at a ({self.path}:1:3)",
        )
        self.assertIn(
            f"  - name is required (at name, {self.at('id: b')})\n"
            f"  - config is required (at config, {self.at('id: b')})",
            self.error("b"),
        )
        with self.assertRaises(ValueError):
            include.tree().resolve("a")

    async def test_entry_fields_accept_an_expression_for_disabled_only(self) -> None:
        """Disabled accepts expressions while a name expression is an invalid option."""
        include = await self.start(
            '- id: a\n  name: config-plugin\n  disabled: !!js "false"\n'
            "- id: b\n  name: !!js \"'config-plugin'\"\n"
        )
        position = self.at("\"'config")
        self.assertEqual(len(self.logs()), 1)
        self.assertIn(f"  - name must be a string (at name, {position})", self.error("b"))
        fiber = include.tree().resolve("a").inner_fiber()
        assert fiber is not None
        self.assertFalse(fiber.is_removed())

    async def test_entry_fields_unload_an_entry_that_turns_invalid_without_writing_to_the_file(
        self,
    ) -> None:
        """Invalid source options retire the instance without persisting a disabled flag."""
        original = "- id: a\n  name: config-plugin\n"
        include = await self.start(original)
        fiber = include.tree().resolve("a").inner_fiber()
        assert fiber is not None
        invalid = original + "  group: 1\n"
        await self.refresh(include, invalid)
        self.assertIn(f"group must be a boolean (at group, {self.at('1')})", self.error())
        with self.assertRaises(ValueError):
            include.tree().resolve("a")
        self.assertTrue(fiber.is_removed())
        self.assertEqual(self.path.read_text(), invalid)
        await self.refresh(include, original)
        revived = include.tree().resolve("a").inner_fiber()
        assert revived is not None
        self.assertFalse(revived.is_removed())

    async def test_entry_fields_locate_entries_nested_in_groups(self) -> None:
        """A collection-valued invalid field uses its key and includes its parent frame."""
        await self.start(
            "- id: g\n  name: group\n  group: true\n  config:\n"
            "    - id: a\n      name: [config-plugin]\n"
        )
        self.assertEqual(
            self.logs(),
            [
                "invalid entry options:\n"
                f"  - name must be a string (at name, {self.at('name: [')})\n"
                f"    at a ({self.path}:5:7)\n    at g ({self.path}:1:3)",
            ],
        )

    async def test_entry_fields_reject_a_file_with_an_entry_that_is_not_an_object(self) -> None:
        """Invalid rows reject the document as a whole and preserve the previous tree."""
        include = await self.start("- id: a\n  name: config-plugin\n")
        fiber = include.tree().resolve("a").inner_fiber()
        await self.refresh(
            include,
            "- id: a\n  name: config-plugin\n- config-plugin\n"
            "- id: g\n  name: group\n  group: true\n  config: [null]\n",
        )
        warnings = self.logs(Level.Warn)
        self.assertEqual(len(warnings), 1)
        self.assertIn(
            f"  - entry must be an object (at {self.path}:3:3)\n"
            f"  - entry must be an object (at {self.at('null')})",
            warnings[0],
        )
        self.assertIs(include.tree().resolve("a").inner_fiber(), fiber)
        with self.assertRaises(ValueError):
            include.tree().resolve("g")

    async def test_entry_fields_leave_invalid_options_passed_at_runtime_unlocated(self) -> None:
        """Rejected runtime options carry a logical path without claiming a source position."""
        include = await self.start("- id: a\n  name: config-plugin\n")
        with self.assertRaises(Exception) as caught:
            await include.tree().update("a", {"disabled": "yes"})
        self.assertEqual(
            str(caught.exception),
            "invalid entry options:\n  - disabled must be a boolean (at disabled)",
        )

    async def test_plugin_import_points_a_plugin_that_cannot_be_found_at_its_name(self) -> None:
        """Unresolved module and package names both carry their name and entry positions."""
        await self.start("- id: a\n  name: ./missing-plugin\n- id: b\n  name: missing-package\n")
        self.assertEqual(len(self.logs()), 2)
        self.assertEqual(
            self.error("b"),
            f"Cannot find plugin 'missing-package' (at name, {self.at('missing-package')})\n"
            f"    at b ({self.path}:3:3)",
        )
        self.assertIn(f"(at name, {self.at('./missing')})", self.error())

    async def test_plugin_import_leaves_entries_outside_any_file_untraced(self) -> None:
        """Root imports report only the catalog failure."""
        await self.start("[]\n")
        await self.loader.create({"name": "missing-plugin"})
        await self.loader.settled()
        self.assertEqual(self.logs(), ["Cannot find plugin 'missing-plugin'"])

    async def expression_failure(self, lines: str, label: str) -> None:
        """Compare the failed token and entry frame for one expression-bearing field."""
        await self.start(f"- id: a\n  name: schema-plugin\n{lines}\n")
        self.assertEqual(len(self.logs()), 1)
        self.assertTrue(
            self.error().endswith(
                f"\n    at {label} ({self.at('foo')})\n    at a ({self.path}:1:3)"
            ),
            self.error(),
        )

    async def test_expressions_point_a_failure_in_inject_at_the_expression(self) -> None:
        """Injection expression failures point at their token."""
        await self.expression_failure('  inject:\n    loader: !!js "1 + foo"', "inject.loader")

    async def test_expressions_point_a_failure_in_disabled_at_the_expression(self) -> None:
        """Disabled expression failures point at their token."""
        await self.expression_failure('  disabled: !!js "1 + foo"', "disabled")

    async def test_expressions_point_a_failure_in_intercept_at_the_expression(self) -> None:
        """Nested interception expression failures retain the full logical path."""
        await self.expression_failure(
            '  intercept:\n    loader: { await: !!js "1 + foo" }', "intercept.loader.await"
        )

    async def test_expressions_point_a_failure_in_config_at_the_expression(self) -> None:
        """Configuration expression failures point at their token before validation."""
        await self.expression_failure('  config:\n    port: !!js "1 + foo"', "config.port")

    async def test_expressions_map_positions_across_lines_and_escapes(self) -> None:
        """Offsets account for double-quote escapes and doubled single quotes."""
        await self.start(
            '- id: b\n  name: config-plugin\n  disabled: !!js "\\"it\'s\\" + \\u0020 foo"\n'
            "- id: c\n  name: config-plugin\n  disabled: !!js '\"it''s\" + bar'\n"
        )
        self.assertEqual(len(self.logs()), 2)
        self.assertIn(f"    at disabled ({self.at('foo')})\n", self.error("b"))
        self.assertIn(f"    at disabled ({self.at('bar')})\n", self.error("c"))

    async def test_expressions_point_an_expression_that_does_not_parse_at_its_start(self) -> None:
        """Syntax failures use the beginning of the decoded expression."""
        await self.start('- id: a\n  name: config-plugin\n  disabled: !!js "foo bar"\n')
        self.assertTrue(
            self.error().endswith(
                f"\n    at disabled ({self.at('foo bar')})\n    at a ({self.path}:1:3)"
            )
        )

    async def test_expressions_locate_entries_nested_in_groups(self) -> None:
        """An expression in a group retains token, child and parent frames."""
        await self.start(
            "- id: g\n  name: group\n  group: true\n  config:\n"
            '    - id: a\n      name: config-plugin\n      disabled: !!js "foo"\n'
        )
        self.assertTrue(
            self.error().endswith(
                f"\n    at disabled ({self.at('foo')})\n"
                f"    at a ({self.path}:5:7)\n    at g ({self.path}:1:3)"
            )
        )

    async def test_expressions_locate_an_expression_written_as_an_object_in_json(self) -> None:
        """JSON expression markers locate inside their string and frame the entry object."""
        source = json.dumps(
            [{"id": "a", "name": "config-plugin", "disabled": {"__jsExpr": "1 + foo"}}], indent=2
        )
        await self.start(source, "location.json")
        position = self.at('{\n    "id"')
        self.assertTrue(
            self.error().endswith(f"\n    at disabled ({self.at('foo')})\n    at a ({position})")
        )

    async def test_expressions_point_a_failure_on_update_at_the_new_expression(self) -> None:
        """Rejected source updates keep the old fiber but report the new file location."""
        include = await self.start("- id: a\n  name: schema-plugin\n  config:\n    port: 80\n")
        fiber = include.tree().resolve("a").inner_fiber()
        assert fiber is not None
        await self.refresh(
            include, '- id: a\n  name: schema-plugin\n  config:\n    port: !!js "1 + foo"\n'
        )
        self.assertTrue(
            self.error().endswith(
                f"\n    at config.port ({self.at('foo')})\n    at a ({self.path}:1:3)"
            )
        )
        self.assertIs(include.tree().resolve("a").inner_fiber(), fiber)
        self.assertFalse(fiber.is_removed())

    async def test_expressions_leave_an_expression_changed_at_runtime_unlocated(self) -> None:
        """An unpersisted runtime expression has no token location but retains its entry frame."""
        include = await self.start("- id: a\n  name: config-plugin\n")
        self.path.chmod(0o444)
        await include.tree().update("a", {"disabled": {"__jsExpr": "foo"}})
        await include.refresh()
        await self.loader.settled()
        self.path.chmod(0o644)
        self.assertTrue(self.error().endswith(f"\n    at disabled\n    at a ({self.path}:1:3)"))

    async def test_expressions_leave_entries_outside_any_file_untraced(self) -> None:
        """Root runtime expressions report failure without file or field source frames."""
        await self.start("[]\n")
        await self.loader.create({"name": "config-plugin", "disabled": {"__jsExpr": "foo"}})
        await self.loader.settled()
        self.assertEqual(len(self.logs()), 1)
        error = self.logs()[0]
        self.assertIn("foo", error)
        self.assertNotIn(" (at ", error)
        self.assertNotIn("\n    at ", error)

    async def test_points_parse_errors_at_the_source(self) -> None:
        """Unknown tags surface as a located parse warning."""
        include = await self.start("[]\n")
        await self.refresh(include, "- id: a\n  name: !foo schema-plugin\n")
        warnings = self.logs(Level.Warn)
        self.assertEqual(len(warnings), 1)
        self.assertIn(f"  - unresolved tag: !foo (at {self.at('!foo')})", warnings[0])

    async def test_json_points_a_validation_issue_at_the_value_in_the_file(self) -> None:
        """JSON invalid config values and entry objects have exact positions."""
        await self.start(
            json.dumps([{"id": "a", "name": "schema-plugin", "config": {"port": "80"}}], indent=2),
            "location.json",
        )
        position, entry = self.at('"80"'), self.at('{\n    "id"')
        self.assertEqual(
            self.logs(),
            [
                "invalid config:\n"
                f"  - port must be a number (at port, {position})\n    at a ({entry})",
            ],
        )

    async def test_json_points_syntax_errors_at_the_source(self) -> None:
        """JSON parse diagnostics retain one normalized source position."""
        include = await self.start("[]\n", "location.json")
        await self.refresh(include, '[\n  { "id": "a", }\n]\n')
        warnings = self.logs(Level.Warn)
        self.assertEqual(len(warnings), 1)
        position = self.at("}\n]")
        self.assertIn(f"(at {position})", warnings[0])
        self.assertNotIn("at position", warnings[0])
        self.assertNotIn("at line", warnings[0])

    async def test_json_points_a_non_array_file_at_its_value(self) -> None:
        """Top-level shape validation locates the actual object after leading whitespace."""
        include = await self.start("[]\n", "location.json")
        await self.refresh(include, '\n{ "id": "a" }\n')
        warnings = self.logs(Level.Warn)
        self.assertEqual(len(warnings), 1)
        self.assertIn(f"  - config file must be a top-level array (at {self.at('{')})", warnings[0])
