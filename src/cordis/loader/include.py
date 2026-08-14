"""File-backed loader tree (port of ``@cordisjs/plugin-include``).

Reads a YAML or JSON file, turns it into loader entries, and writes updates
back when the file is writable. Supports ``patches`` (insert/override entries)
and ``!py`` expression interpolation — the Python rendering of the TS ``!!js``
config-file expression mechanism.

Trust + round-trip notes:

* ``!py`` evaluates an arbitrary Python expression (with ``os.environ`` in
  scope) — a config file is therefore code. Treat it as trusted, same as the
  TS ``!!js`` tag it renders.
* Write-back (``ctx._include_write``) serializes the *evaluated* value, so a
  ``!py`` expression is NOT preserved on round-trip (its marker is lost). This
  is a documented limitation of the best-effort write-back path; the source of
  truth remains the authored file.
"""

from __future__ import annotations

import json
import os
from typing import Any

import yaml


class _ExprLoader(yaml.SafeLoader):
    pass


def _py_construct(loader: yaml.Loader, node: yaml.Node) -> Any:
    expr = loader.construct_scalar(node)
    env = dict(os.environ)
    try:
        return eval(expr, {"__builtins__": {"__import__": __import__}}, env)  # noqa: S307
    except Exception:  # noqa: BLE001
        return None


_ExprLoader.add_constructor("!py", _py_construct)
_ExprLoader.add_constructor("tag:yaml.org,2002:js", _py_construct)


def _read_file(path: str, initial: list) -> list:
    if not os.path.exists(path):
        if initial:
            _write_file(path, initial)
        return list(initial)
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    if path.endswith((".json", ".jsonc")):
        return json.loads(text)
    return yaml.load(text, Loader=_ExprLoader) or []


def _write_file(path: str, entries: list) -> None:
    directory = os.path.dirname(path) or "."
    if not os.access(directory, os.W_OK):
        return
    with open(path, "w", encoding="utf-8") as fh:
        if path.endswith((".json", ".jsonc")):
            json.dump(entries, fh, indent=2)
        else:
            yaml.safe_dump(entries, fh, sort_keys=False)


def _apply_patches(entries: list, patches: list) -> list:
    by_id: dict[str, dict] = {e.get("id"): e for e in entries if e.get("id") is not None}
    for patch in patches or []:
        pid = patch.get("id")
        if pid in by_id:
            merged = dict(by_id[pid])
            merged.update(patch)
            by_id[pid] = merged
            for i, e in enumerate(entries):
                if e.get("id") == pid:
                    entries[i] = merged
                    break
        else:
            entries.append(patch)
            by_id[pid] = patch
    return entries


async def include(ctx: Any, config: Any = None) -> None:
    """Read a config file and graft its entries into the loader tree."""
    config = config or {}
    path = config["path"]
    initial = config.get("initial", [])
    patches = config.get("patches", [])

    entries = _read_file(path, initial)
    entries = _apply_patches(entries, patches)

    for entry in entries:
        await ctx.loader.create(entry)

    # write back is best-effort: the include plugin does not own the loader's
    # change notifications in this port; expose write() for explicit saves.
    def write() -> None:
        tree = []
        for child in ctx.loader._root.children:  # noqa: SLF001
            opts = child.to_options()
            if opts.get("config") is None:
                opts.pop("config", None)
            tree.append(opts)
        _write_file(path, tree)

    ctx._include_write = write  # type: ignore[attr-defined]


include.name = "@cordisjs/plugin-include"  # type: ignore[attr-defined]
include.inject = ["loader"]  # type: ignore[attr-defined]


__all__ = ["include"]
