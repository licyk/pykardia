"""Core runtime entry points."""

from pykardia.core.cleanup import Disposal, Disposer
from pykardia.core.config import ErasedConfig, Issue, NoConfig, PathSegment, ValidationError, decode
from pykardia.core.context import Context, merge_shallow
from pykardia.core.deps import Deps, Injected
from pykardia.core.effect import EffectHandle, EffectMeta, EffectScope
from pykardia.core.error import (
    AccessError,
    AggregateError,
    AlreadyProvided,
    Error,
    ForeignBinding,
    Inactive,
    InactiveEffect,
    Interrupted,
    NotProvided,
    Panicked,
    PyKardiaError,
    TypeMismatch,
    Undeclared,
)
from pykardia.core.events import (
    ConfigCommitted,
    Event,
    PluginInserted,
    PluginRemoved,
    Replaced,
    ServiceChanged,
    StatusChanged,
)
from pykardia.core.fiber import Fiber, Settle
from pykardia.core.id_table import IdTable
from pykardia.core.key import FiberId, ImplId, Key, RealmId
from pykardia.core.logger import Exporter, Level, Logger, Message, hyphenate
from pykardia.core.plugin import Inject, Plugin, PluginDef, RuntimeKey
from pykardia.core.registry import Registry
from pykardia.core.scope import Overlay
from pykardia.core.service import Service, Svc
from pykardia.core.state import Check
from pykardia.core.status import FiberState
from pykardia.core.value import JsonValue

__all__: list[str] = [
    "AccessError",
    "AggregateError",
    "AlreadyProvided",
    "Check",
    "ConfigCommitted",
    "Context",
    "Deps",
    "Disposal",
    "Disposer",
    "EffectHandle",
    "EffectMeta",
    "EffectScope",
    "ErasedConfig",
    "Error",
    "Event",
    "Exporter",
    "Fiber",
    "FiberId",
    "FiberState",
    "ForeignBinding",
    "IdTable",
    "ImplId",
    "Inactive",
    "InactiveEffect",
    "Inject",
    "Injected",
    "Interrupted",
    "Issue",
    "JsonValue",
    "PyKardiaError",
    "Key",
    "Level",
    "Logger",
    "Message",
    "NoConfig",
    "NotProvided",
    "Overlay",
    "Panicked",
    "PathSegment",
    "Plugin",
    "PluginDef",
    "PluginInserted",
    "PluginRemoved",
    "RealmId",
    "Registry",
    "Replaced",
    "RuntimeKey",
    "Service",
    "ServiceChanged",
    "Settle",
    "StatusChanged",
    "Svc",
    "TypeMismatch",
    "Undeclared",
    "ValidationError",
    "decode",
    "hyphenate",
    "merge_shallow",
]
