"""Hot module replacement (port of ``@cordisjs/plugin-hmr``).

HMR applies the revertible-effect pattern at the module level: disposing an old
fiber recovers what the component installed, and a fresh fiber from the reloaded
module reinstalls it — no developer-annotated acceptance boundaries.

Three phases mirror the paper's Algorithms 8–10:

1. **classify** — a fixed point over the import graph partitions changed modules
   into *accepted* (hot-replaceable) and *declined* (force a full restart).
2. **detect** — an entry is stale iff its import tree intersects *accepted*.
3. **transactional reload** — invalidate caches, dispose+reimport stale entries;
   on failure restore caches and rebuild from backup so the system never enters a
   half-reloaded state.

In Python the module cache is ``sys.modules`` and reload is ``importlib.reload``
/ re-import. Framework-level dependencies (``externals``) fall back to
``loader.exit()``.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import os
import sys
from typing import Any

from ..service import Service
from ..utils import symbols
from .loader import RestartRequired


def _module_file(name: str) -> str | None:
    spec = importlib.util.find_spec(name)
    if spec is None or spec.origin in (None, "built-in", "frozen"):
        return None
    return spec.origin


def _get_imports(name: str) -> list[str]:
    """Return the direct import dependencies of a loaded module."""
    module = sys.modules.get(name)
    if module is None:
        return []
    deps: list[str] = []
    seen = set()
    for value in vars(module).values():
        mod = getattr(value, "__name__", None)
        if isinstance(mod, str) and mod not in seen and mod in sys.modules:
            seen.add(mod)
            deps.append(mod)
    return deps


class HmrService(Service):
    """Hot module replacement for loader-managed Cordis plugins."""

    provide = "hmr"
    inject = ["loader", "timer"]

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        config = config or {}
        self._root = config.get("root", ["."])
        self._ignored = config.get("ignored", ["**/node_modules", "**/.*"])
        self._debounce_ms = config.get("debounce", 100)
        self._externals: set[str] = set()
        self._watching = False
        self._task: asyncio.Task | None = None
        self._mtimes: dict[str, float] = {}
        # channel for RestartRequired to reach the host (the watch task is
        # fire-and-forget, so an unhandled exception would only surface as a
        # "Task exception was never retrieved" warning). The host awaits
        # ``restart_event`` / reads ``restart_error``.
        self._restart_error: RestartRequired | None = None
        self.restart_event: asyncio.Event = asyncio.Event()
        ctx.reflect.mixin("hmr", ["watch", "reload"])

    # ------------------------------------------------------------------
    # phase 1: classify
    # ------------------------------------------------------------------

    def classify(self, stashed: set[str], externals: set[str]) -> tuple[set[str], set[str]]:
        """Fixed-point classify changed modules into (accepted, declined).

        Changed (stashed) modules are accepted by default — they are the ones to
        reload — unless they are external. Acceptance then propagates to
        importers: a module is accepted if any module it imports is accepted.
        Externals (framework-level dependencies) are declined and force a full
        restart via ``loader.exit()``.
        """
        accepted: set[str] = set()
        declined: set[str] = set(externals)
        for name in stashed:
            if name in externals:
                declined.add(name)
            else:
                accepted.add(name)

        changed = True
        while changed:
            changed = False
            for name in list(sys.modules):
                if name in accepted or name in declined:
                    continue
                deps = _get_imports(name)
                if any(d in accepted for d in deps):
                    accepted.add(name)
                    changed = True
        return accepted, declined

    # ------------------------------------------------------------------
    # phase 2: detect stale entries
    # ------------------------------------------------------------------

    def get_dependencies(self, root: str, declined: set[str]) -> set[str]:
        """Collect transitive imports of ``root`` respecting declined as boundary."""
        seen: set[str] = set()
        stack = [root]
        while stack:
            name = stack.pop()
            if name in seen or name in declined:
                continue
            seen.add(name)
            for dep in _get_imports(name):
                if dep not in declined:
                    stack.append(dep)
        return seen

    def detect(self, accepted: set[str], entries: list) -> list:
        """An entry is stale iff its import tree intersects accepted."""
        stale = []
        for entry in entries:
            if entry.name is None:
                continue
            deps = self.get_dependencies(entry.name, set())
            if deps & accepted:
                stale.append(entry)
        return stale

    # ------------------------------------------------------------------
    # phase 3: transactional reload
    # ------------------------------------------------------------------

    async def reload(self, accepted: set[str], stale_entries: list) -> None:
        """Invalidate caches, dispose + reimport stale entries; restore on error."""
        backup: dict[str, Any] = {}
        for name in accepted:
            mod = sys.modules.get(name)
            if mod is not None:
                backup[name] = mod
                del sys.modules[name]

        try:
            for entry in stale_entries:
                if entry.fiber is not None:
                    task = entry.fiber.dispose()
                    if task is not None:
                        await task
                    entry.fiber = None
                # reimport (fresh module object)
                module = importlib.import_module(entry.name)
                from .loader import resolve_plugin

                entry.runtime = resolve_plugin(module)
                entry.fiber = await entry.ctx.plugin(entry.runtime, entry.config)
        except BaseException:
            # restore caches and rebuild from backup
            for name, mod in backup.items():
                sys.modules[name] = mod
            for entry in stale_entries:
                if entry.fiber is None and entry.name in backup:
                    module = backup[entry.name]
                    from .loader import resolve_plugin

                    entry.runtime = resolve_plugin(module)
                    entry.fiber = await entry.ctx.plugin(entry.runtime, entry.config)
            raise

    # ------------------------------------------------------------------
    # file watching (polling)
    # ------------------------------------------------------------------

    def watch(self) -> None:
        if self._watching:
            return
        self._watching = True
        self._task = asyncio.ensure_future(self._poll())

    @property
    def restart_error(self) -> "RestartRequired | None":
        """The restart request raised by a declined framework-level change."""
        return self._restart_error

    async def _poll(self) -> None:
        roots = [os.path.abspath(r) for r in self._root]
        try:
            while True:
                await asyncio.sleep(self._debounce_ms / 1000)
                changed = self._scan(roots)
                if not changed:
                    continue
                await self._on_change(changed)
        except RestartRequired as exc:
            # surface to the host via the event/error attrs instead of dying as
            # an unretrieved task exception
            self._restart_error = exc
            self.restart_event.set()
            self._watching = False
            self.ctx.logger.warn("hmr: restart requested by framework-level change")
            return

    def _ignored_segment(self, part: str) -> bool:
        """Match a path segment against the ignore patterns (glob-aware)."""
        import fnmatch

        for pat in self._ignored:
            base = pat.split("/")[-1]  # '**/node_modules' -> 'node_modules'
            if part == base or fnmatch.fnmatch(part, base):
                return True
        return False

    def _scan(self, roots: list[str]) -> set[str]:
        changed: set[str] = set()
        for root in roots:
            if not os.path.isdir(root):
                continue
            for dirpath, _dirs, files in os.walk(root):
                if any(self._ignored_segment(part) for part in dirpath.split(os.sep)):
                    continue
                for fname in files:
                    if not fname.endswith(".py"):
                        continue
                    full = os.path.join(dirpath, fname)
                    try:
                        mtime = os.path.getmtime(full)
                    except OSError:
                        continue
                    if full in self._mtimes and self._mtimes[full] != mtime:
                        mod = self._file_to_module(full, root)
                        if mod is not None:
                            changed.add(mod)
                    self._mtimes[full] = mtime
        return changed

    def _file_to_module(self, full: str, root: str) -> str | None:
        rel = os.path.relpath(full, root)
        # __init__.py represents its package, not a submodule named __init__
        if os.path.basename(rel) == "__init__.py":
            mod = os.path.dirname(rel).replace(os.sep, ".")
        else:
            mod = rel[:-3].replace(os.sep, ".")
        return mod if mod else None

    async def _on_change(self, changed: set[str]) -> None:
        accepted, declined = self.classify(changed, self._externals)
        if declined:
            # framework-level dependency: full restart (raises RestartRequired)
            self.ctx.logger.warn("hmr: declined modules %s — falling back to exit", declined)
            self.ctx.loader.exit()
            return
        entries = [e for e in self.ctx.loader._entries.values()]  # noqa: SLF001
        stale = self.detect(accepted, entries)
        if not stale:
            self.ctx.emit("hmr/change", changed)
            return
        await self.reload(accepted, stale)
        self.ctx.emit("hmr/reload", stale)


hmr = HmrService
hmr.name = "@cordisjs/plugin-hmr"  # type: ignore[attr-defined]

__all__ = ["HmrService", "hmr"]
