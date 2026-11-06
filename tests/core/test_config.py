"""Configuration ports of core/src/config.rs plus declared Python type adaptations."""

import unittest
from dataclasses import dataclass, field
from enum import Enum
from typing import Annotated

from pykardia.core.config import Issue, NoConfig, ValidationError, decode, decode_json
from pykardia.core.value import JsonValue


@dataclass
class Config:
    """Match the reference test's defaulted list of unsigned 32-bit integers."""

    items: list[Annotated[int, "u32"]] = field(default_factory=list)


class Mode(Enum):
    """Explicit JSON values of a typed configuration enum."""

    ON = "on"
    OFF = "off"


@dataclass
class Nested:
    """Exercise option fields, enums, nested structs and source field aliases."""

    config: Config
    mode: Mode
    enabled: bool = field(metadata={"alias": "isEnabled"})
    label: str | None = None


@dataclass
class RawConfig:
    """Retain recursive raw JSON fields resolved through dataclass type hints."""

    values: dict[str, JsonValue]


class ConfigTests(unittest.TestCase):
    """Check actual decoder results, locations and JSON snapshot ownership."""

    def test_nested_json_alias_in_dataclass(self) -> None:
        """Expanded recursive forward references remain JSON snapshots at any depth."""
        value: JsonValue = {"values": {"nested": [{"items": [True, 1, 1.5, None]}]}}
        result = decode(value, RawConfig)
        self.assertEqual(result.values, {"nested": [{"items": [True, 1, 1.5, None]}]})
        result.values["nested"] = None
        self.assertEqual(value, {"values": {"nested": [{"items": [True, 1, 1.5, None]}]}})

    def test_formats_issues_with_paths(self) -> None:
        """Port config::formats_issues_with_paths exactly."""
        error = ValidationError([Issue("not a number").at(["items", 0]), Issue("required")])
        self.assertEqual(str(error), "invalid config:\n  - not a number (at items.0)\n  - required")

    def test_decodes_with_paths_and_defaults(self) -> None:
        """Port config::decodes_with_paths_and_defaults."""
        with self.assertRaises(ValidationError) as caught:
            decode({"items": [1, "x"]}, Config)
        self.assertEqual(caught.exception.issues[0].format_path(), "items.1")
        self.assertEqual(
            caught.exception.issues[0].message, 'invalid type: string "x", expected u32'
        )
        self.assertEqual(decode(None, Config), Config([]))
        self.assertEqual(decode({"unknown": True}, Config), Config([]))
        self.assertIsNone(decode({"anything": 1}, NoConfig))

    def test_nested_alias_enum_optional_and_unknown_fields(self) -> None:
        """Decode only declared fields, retaining each field's actual type."""
        result = decode(
            {"config": {"items": [1]}, "mode": "on", "isEnabled": True, "unknown": 42}, Nested
        )
        self.assertEqual(result, Nested(Config([1]), Mode.ON, True))
        with self.assertRaises(ValidationError) as caught:
            decode({"config": {"items": [False]}, "mode": "on", "isEnabled": True}, Nested)
        self.assertEqual(caught.exception.issues[0].path, ["config", "items", 0])

    def test_numeric_types_and_declared_widths(self) -> None:
        """Do not accept booleans as integers or coerce strings into typed fields."""
        for value in [False, "1", 1.0, -1, 2**32]:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                decode({"items": [value]}, Config)
        self.assertEqual(decode({"items": [0, 2**32 - 1]}, Config).items, [0, 2**32 - 1])
        self.assertEqual(decode(1, float), 1.0)
        with self.assertRaises(ValidationError):
            decode(1, bool)

    def test_defaults_and_json_snapshots_do_not_share_mutable_containers(self) -> None:
        """Preserve Rust clone boundaries without sharing a dataclass default or raw JSON."""
        first = decode(None, Config)
        second = decode(None, Config)
        first.items.append(1)
        self.assertEqual(second.items, [])
        source: JsonValue = {"unknown": [1]}
        copied = decode_json(source)
        assert isinstance(copied, dict)
        assert isinstance(copied["unknown"], list)
        copied["unknown"].append(2)
        self.assertEqual(source, {"unknown": [1]})
        self.assertIsNone(decode_json(None))

    def test_missing_fields_and_null_retry_report_actual_target(self) -> None:
        """A null retries as an object, while an already valid nullable field remains null."""
        with self.assertRaises(ValidationError) as caught:
            decode(None, Nested)
        self.assertEqual(caught.exception.issues[0].message, "missing field `config`")
        self.assertEqual(caught.exception.issues[0].path, [])
        value = decode({"config": {}, "mode": "off", "isEnabled": False, "label": None}, Nested)
        self.assertIsNone(value.label)
        self.assertIsNone(decode(None, str | None))
        self.assertEqual(decode("text", str | None), "text")
