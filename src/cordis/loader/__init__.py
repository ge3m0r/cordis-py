"""Cordis component loader family (port of the ``@cordisjs/plugin-*`` packages).

* :mod:`cordis.loader.loader` — runtime plugin loader (``ctx.loader``).
* :mod:`cordis.loader.include` — file-backed YAML/JSON config tree.
* :mod:`cordis.loader.hmr` — hot module replacement.
* :mod:`cordis.loader.group` — nested plugin groups.
"""

from __future__ import annotations

from .loader import LoaderService, Entry, resolve_plugin, RestartRequired
from .include import include
from .hmr import HmrService, hmr
from .group import group

__all__ = [
    "LoaderService",
    "Entry",
    "resolve_plugin",
    "RestartRequired",
    "include",
    "HmrService",
    "hmr",
    "group",
]
