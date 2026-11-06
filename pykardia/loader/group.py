"""Structural group plugin with in-place entry reconciliation."""

from pykardia.core.cleanup import Disposer
from pykardia.core.context import Context
from pykardia.core.plugin import PluginDef
from pykardia.core.value import JsonValue
from pykardia.loader.loader import Loader

GROUP_TAG = "loader:group"


def group_plugin() -> PluginDef:
    """Create a group definition whose configuration is its ordered child entry list."""

    async def apply(ctx: Context, config: JsonValue) -> None:
        """Create the child group and retain both its update hook and stop inverse."""
        loader = ctx.get(Loader).arc()
        group = loader.new_subgroup(ctx)
        ctx.on_update_with(
            JsonValue, lambda value: group.update(value if isinstance(value, list) else [])
        )
        ctx.defer("group", Disposer.new(group.stop))
        await group.update(config if isinstance(config, list) else [])

    return PluginDef.new(apply).name("group").tag(GROUP_TAG)
