"""Nested plugin groups (port of ``@cordisjs/plugin-group``).

A group plugin takes a list of child entries as its configuration and loads
them as a subgroup beneath the entry that owns it. The loader also supports
``group: true`` entries as first-class containers; this plugin covers the
``name: '@cordisjs/plugin-group'`` form.
"""

from __future__ import annotations

from typing import Any


async def group(ctx: Any, config: Any = None) -> None:
    """Load each child entry in ``config`` beneath the owning entry."""
    parent_id = ctx.loader.locate(ctx.fiber)
    for child in config or []:
        await ctx.loader.create(child, parent=parent_id)


group.name = "@cordisjs/plugin-group"  # type: ignore[attr-defined]
group.inject = ["loader"]  # type: ignore[attr-defined]


__all__ = ["group"]
