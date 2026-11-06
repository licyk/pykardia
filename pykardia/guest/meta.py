"""Metadata builders for guest modules and their declared service access."""

from __future__ import annotations

from dataclasses import dataclass, field

from pykardia.core.value import JsonValue


@dataclass
class PluginMeta:
    """The guest plugin's optional name, dependency keys and provided service keys."""

    name: str | None = None
    inject: list[str] = field(default_factory=list)
    provide: list[str] = field(default_factory=list)


@dataclass
class Meta:
    """A guest module's static dependencies, optional plugin and optional JSON export."""

    deps: list[str] = field(default_factory=list)
    plugin_meta: PluginMeta | None = None
    exported_value: JsonValue = None
    has_value: bool = False

    @classmethod
    def plugin(cls, name: str) -> Meta:
        """Declare a named plugin export."""
        return cls(plugin_meta=PluginMeta(name=name))

    @classmethod
    def value(cls, value: JsonValue) -> Meta:
        """Declare a JSON value export, retaining an explicit null in serialized metadata."""
        return cls(exported_value=value, has_value=True)

    def dep(self, path: str) -> Meta:
        """Append a static dependency path relative to this module."""
        self.deps.append(path)
        return self

    def inject(self, key: str) -> Meta:
        """Declare required service access, creating unnamed plugin metadata when needed."""
        if self.plugin_meta is None:
            self.plugin_meta = PluginMeta()
        self.plugin_meta.inject.append(key)
        return self

    def provide(self, key: str) -> Meta:
        """Declare a provided service, creating unnamed plugin metadata when needed."""
        if self.plugin_meta is None:
            self.plugin_meta = PluginMeta()
        self.plugin_meta.provide.append(key)
        return self

    def to_json(self) -> JsonValue:
        """Render the exact original metadata fields without serializing Python builder names."""
        result: dict[str, JsonValue] = {"deps": list(self.deps)}
        if self.plugin_meta is not None:
            result["plugin"] = {
                "name": self.plugin_meta.name,
                "inject": list(self.plugin_meta.inject),
                "provide": list(self.plugin_meta.provide),
            }
        if self.has_value:
            result["value"] = self.exported_value
        return result
