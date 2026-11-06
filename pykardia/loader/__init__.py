"""Declarative plugin loading and source-preserving entry options."""

from pykardia.loader import expr
from pykardia.loader.backend import (
    MISSING,
    EntryChange,
    Location,
    MemoryTree,
    TreeBackend,
    value_at,
)
from pykardia.loader.catalog import Catalog, ModuleNotFound, Resolver, builtins
from pykardia.loader.group import GROUP_TAG, group_plugin
from pykardia.loader.handles import EntryHandle, GroupHandle, TreeHandle
from pykardia.loader.loader import OUTER_TAG, Loader, PatchNext
from pykardia.loader.options import (
    EXPR_KEY,
    SEP,
    Disabled,
    EntryOptions,
    EntryValidationError,
    IsolateLabel,
    diff_keys,
    expr_source,
    is_expr,
    merge,
    sort_keys,
    validate_options,
)

__all__: list[str] = [
    "Catalog",
    "Disabled",
    "EXPR_KEY",
    "EntryChange",
    "EntryHandle",
    "EntryOptions",
    "EntryValidationError",
    "GROUP_TAG",
    "GroupHandle",
    "IsolateLabel",
    "Loader",
    "Location",
    "MISSING",
    "MemoryTree",
    "ModuleNotFound",
    "OUTER_TAG",
    "PatchNext",
    "Resolver",
    "SEP",
    "TreeBackend",
    "TreeHandle",
    "builtins",
    "diff_keys",
    "expr",
    "expr_source",
    "group_plugin",
    "is_expr",
    "merge",
    "sort_keys",
    "validate_options",
    "value_at",
]
