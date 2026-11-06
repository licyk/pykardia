"""Plugin-name resolution and shared static catalogs."""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from collections.abc import Awaitable
from typing import TypeVar

from pykardia.core.plugin import Plugin, PluginDef
from pykardia.core.runtime import Completion

C = TypeVar("C")


class Resolver(ABC):
    """Resolve an entry name to a plugin definition, optionally asynchronously."""

    @abstractmethod
    def resolve(self, name: str) -> Awaitable[PluginDef]:
        """Return the definition or fail with a resolution error when awaited."""


class ModuleNotFound(Exception):
    """No plugin is registered for an entry's requested name."""

    def __init__(self, name: str) -> None:
        """Retain the unresolved name and the source diagnostic."""
        self.name = name
        super().__init__(f"Cannot find plugin '{name}'")


class Catalog(Resolver):
    """A mutable plugin registry whose aliases snapshot definitions at registration."""

    def __init__(self) -> None:
        """Create a shared catalog handle with no registered plugins."""
        self._plugins: dict[str, PluginDef] = {}
        self._lock = threading.Lock()

    @classmethod
    def new(cls) -> Catalog:
        """Create an empty catalog."""
        return cls()

    def register(self, plugin: type[Plugin[C]]) -> Catalog:
        """Register a typed plugin under its declared NAME and return this catalog."""
        return self.register_def(plugin.NAME, PluginDef.of(plugin))

    def register_def(self, name: str, definition: PluginDef) -> Catalog:
        """Insert or replace one name without altering definitions already captured by aliases."""
        with self._lock:
            self._plugins[name] = definition
        return self

    def alias(self, alias: str, name: str) -> Catalog:
        """Snapshot a current definition under another name; a missing target is ignored."""
        definition = self.get(name)
        if definition is not None:
            self.register_def(alias, definition)
        return self

    def get(self, name: str) -> PluginDef | None:
        """Read the currently registered definition without triggering an import."""
        with self._lock:
            return self._plugins.get(name)

    def resolve(self, name: str) -> Completion[PluginDef]:
        """Snapshot resolution synchronously, returning an already completed observer."""
        definition = self.get(name)
        if definition is None:
            return Completion.failed(ModuleNotFound(name))
        return Completion.ready(definition)


def builtins() -> Catalog:
    """Create the loader's built-in group catalog and its Cordis alias."""
    from pykardia.loader.group import group_plugin

    catalog = Catalog.new()
    catalog.register_def("group", group_plugin())
    catalog.alias("@cordisjs/plugin-group", "group")
    return catalog
