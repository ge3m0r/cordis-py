"""Runtime plugin loader (port of ``@cordisjs/plugin-loader``).

The loader owns an ``EntryTree``, imports plugin modules by name, applies their
config, and keeps the running plugin graph in sync with entry updates. Entry
fields mirror the paper's Definition 74: ``id`` (reconciliation key), ``name``
(module specifier), ``config``, ``group``, ``disabled``, ``inject``.

Reconciliation is incremental and per-field (Section 5.2.1): ``id``/``name``
rebuild the entry; ``inject`` restarts; ``config`` is handed to the component
(via ``fiber.update``); ``disabled`` unloads/reloads; group entries diff their
child list by id.
"""

from __future__ import annotations

import asyncio
import importlib
from typing import Any

from ..service import Service

if False:  # TYPE_CHECKING
    from ..context import Context
    from ..fiber import Fiber


class Entry:
    """One loader entry: a declarative plugin slot in the entry tree."""

    __slots__ = (
        "id", "name", "config", "group", "disabled", "inject",
        "parent", "children", "ctx", "fiber", "runtime",
    )

    def __init__(self, options: dict) -> None:
        self.id = options.get("id")
        self.name = options.get("name")
        self.config = options.get("config", {})
        self.group = bool(options.get("group", False))
        self.disabled = bool(options.get("disabled", False))
        self.inject = options.get("inject")
        self.parent: "Entry | None" = None
        self.children: list[Entry] = []
        self.ctx: "Context | None" = None
        self.fiber: "Fiber | None" = None
        self.runtime: Any = None  # resolved plugin object

    def to_options(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "config": self.config,
            "group": self.group,
            "disabled": self.disabled,
            "inject": self.inject,
        }


def resolve_plugin(module: Any) -> Any:
    """Resolve a module to its plugin entrypoint (default export convention)."""
    for attr in ("plugin", "default", "apply"):
        value = getattr(module, attr, None)
        if value is not None:
            return value
    if callable(module):
        return module
    return module


class RestartRequired(Exception):
    """Raised by ``loader.exit()`` when a framework-level change needs a host restart.

    Python cannot cleanly re-exec a live module graph the way Node restarts a
    process; the host is expected to catch this and re-launch. HMR raises it on
    the declined-external path so the fallback is never silent.
    """


class LoaderService(Service):
    """Runtime plugin loader installed as ``ctx.loader``."""

    provide = "loader"

    def __init__(self, ctx: "Context", config: Any = None) -> None:
        super().__init__(ctx)
        self._entries: dict[str, Entry] = {}
        self._root = Entry({"id": None, "group": True})
        self._root.ctx = ctx
        self._suspended = False
        ctx.reflect.mixin("loader", ["create", "update", "remove", "resolve", "resolveGroup", "locate", "await_"])

    # ------------------------------------------------------------------
    # tree navigation
    # ------------------------------------------------------------------

    def resolve(self, id: str) -> "Entry | None":
        """Resolve an entry by id, including nested ``a:b`` ids."""
        if id is None:
            return self._root
        parts = id.split(":") if isinstance(id, str) else [id]
        node = self._root
        for part in parts:
            nxt = next((c for c in node.children if c.id == part), None)
            if nxt is None:
                return None
            node = nxt
        return node

    def resolve_group(self, id: str | None) -> "Entry":
        entry = self.resolve(id) if id is not None else self._root
        if entry is None:
            raise KeyError(f"entry not found: {id}")
        if not entry.group:
            entry = entry.parent or self._root
        return entry

    def locate(self, fiber: "Fiber | None" = None) -> "str | None":
        """Return the loader entry id that owns a fiber."""
        if fiber is None:
            return None
        for entry in self._entries.values():
            if entry.fiber is fiber:
                return entry.id
        return None

    # ------------------------------------------------------------------
    # entry lifecycle
    # ------------------------------------------------------------------

    async def create(self, options: dict, parent: str | None = None, position: int | None = None) -> str:
        entry = Entry(options)
        if entry.id is None:
            entry.id = f"entry-{len(self._entries) + 1}"
        if entry.id in self._entries:
            raise ValueError(f"entry id already exists: {entry.id}")

        group = self.resolve_group(parent)
        entry.parent = group
        entry.ctx = group.ctx
        if position is None:
            group.children.append(entry)
        else:
            group.children.insert(position, entry)
        self._entries[entry.id] = entry

        await self._start(entry)
        return entry.id

    async def _start(self, entry: Entry) -> None:
        if entry.disabled:
            return
        if entry.group:
            # group entries create a child context for their children
            entry.ctx = (entry.parent.ctx if entry.parent else self.ctx).extend({})
            return
        module = importlib.import_module(entry.name) if entry.name else None
        if module is None:
            return
        plugin = resolve_plugin(module)
        if entry.inject is not None:
            if isinstance(plugin, dict) or hasattr(plugin, "__dict__"):
                try:
                    setattr(plugin, "inject", entry.inject)
                except (AttributeError, TypeError):
                    pass
        entry.runtime = plugin
        entry.fiber = await self._ctx_for(entry).plugin(plugin, entry.config)

    def _ctx_for(self, entry: Entry) -> "Context":
        ctx = entry.ctx or self.ctx
        if entry.inject and isinstance(entry.inject, dict):
            for name, cfg in entry.inject.items():
                if cfg is None:
                    continue
                ctx = ctx.intercept(name, cfg)
        return ctx

    async def _stop(self, entry: Entry) -> None:
        # stop children first (LIFO within the group)
        for child in list(entry.children):
            await self._stop(child)
        if entry.fiber is not None:
            task = entry.fiber.dispose()
            if task is not None:
                await task
            entry.fiber = None

    async def remove(self, id: str) -> None:
        entry = self.resolve(id)
        if entry is None or entry is self._root:
            raise KeyError(f"entry not found: {id}")
        await self._stop(entry)
        if entry.parent is not None:
            entry.parent.children.remove(entry)
        self._entries.pop(entry.id, None)

    async def update(self, id: str, options: dict, parent: str | None = None, position: int | None = None) -> None:
        entry = self.resolve(id)
        if entry is None:
            raise KeyError(f"entry not found: {id}")

        rebuild = False
        if "name" in options and options["name"] != entry.name:
            entry.name = options["name"]
            rebuild = True
        if "disabled" in options:
            new_disabled = bool(options["disabled"])
            if new_disabled != entry.disabled:
                entry.disabled = new_disabled
                if new_disabled:
                    await self._stop(entry)
                else:
                    await self._start(entry)
                return
        if "inject" in options:
            entry.inject = options["inject"]
            rebuild = True

        if "config" in options:
            entry.config = options["config"]
            if entry.fiber is not None and not entry.disabled:
                task = entry.fiber.update(entry.config)
                if asyncio.iscoroutine(task):
                    await task

        if "group" in options and options["group"]:
            # keyed diff over child ids for group config (also for empty groups)
            await self._reconcile_children(entry, options["config"] if isinstance(options.get("config"), list) else [])

        if parent is not None and position is not None and entry.parent is not None:
            entry.parent.children.remove(entry)
            entry.parent.children.insert(position, entry)

        if rebuild and not entry.disabled:
            await self._stop(entry)
            await self._start(entry)

    async def _reconcile_children(self, group: Entry, child_options: list) -> None:
        existing = {c.id: c for c in group.children}
        new_ids = [c.get("id") for c in child_options if c.get("id") is not None]
        # remove children no longer present
        for cid, child in list(existing.items()):
            if cid not in new_ids:
                await self._stop(child)
                group.children.remove(child)
                self._entries.pop(cid, None)
        # add/update in declared order
        for idx, opts in enumerate(child_options):
            cid = opts.get("id")
            if cid in existing and cid in new_ids:
                await self.update(cid, opts, parent=group.id, position=idx)
            else:
                await self.create(opts, parent=group.id, position=idx)

    async def await_(self) -> None:
        """Wait for pending entry imports and fiber reloads."""
        for entry in list(self._entries.values()):
            if entry.fiber is not None and entry.fiber.inertia is not None:
                await entry.fiber.await_()

    def exit(self) -> None:
        """Framework-level restart hook (used by HMR for un-reloadable deps).

        Raises :class:`RestartRequired`; the host catches it and re-launches.
        """
        raise RestartRequired("framework-level dependency changed; host must restart")


__all__ = ["LoaderService", "Entry", "resolve_plugin"]
