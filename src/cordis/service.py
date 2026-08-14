"""Base class for services that expose a named API on ``ctx``.

Faithful port of ``vendor/cordis/src/service.ts``. A ``Service`` subclass calls
``super().__init__(ctx, name)``; the service is registered immediately via
``ctx.reflect.provide`` and is automatically removed when its owning fiber
unloads. The intercept-config resolution (``resolve_config``) walks the
ancestor intercept chain, mirroring the coeffect interception of Definitions
30–31.
"""

from __future__ import annotations

from typing import Any

from .symbols import symbols
from .utils import Tracker, create_callable, get_traceable, is_object

if False:  # TYPE_CHECKING
    from .context import Context


class Service:
    """Base class for services that expose a named API on ``ctx``."""

    # Symbol-keyed API surface (mirrors the TS static symbols).
    init = symbols.init
    check = symbols.check
    config = symbols.config
    invoke = symbols.invoke
    extend = symbols.extend
    tracker = symbols.tracker
    resolveConfig = symbols.resolveConfig

    # A subclass may set a class-level ``provide`` name (TS ``static provide``).
    provide: str | None = None

    def __init__(self, ctx: "Context", name: str | None = None) -> None:
        if name is None:
            name = type(self).provide  # type: ignore[assignment]
        tracker = Tracker(associate=name, property="ctx")
        self.ctx = ctx
        self.name = name
        self._tracker = tracker

        # A service with an ``_invoke`` method is callable (TS createCallable).
        invoke = getattr(self, "_invoke", None)
        if invoke is not None:
            create_callable(name, self, tracker)  # tags _tracker; __call__ dispatches

        ctx.reflect.provide(name, self, getattr(self, "_check", None))

    def _filter(self, ctx: "Context") -> bool:
        """Isolation filter: the accessing ctx must share this service's realm."""
        return ctx._isolate.get(self.name) is self.ctx._isolate.get(self.name)

    def _extend(self, props: dict | None = None):
        """Derive an extended service instance with overlaid properties."""
        import copy

        child = object.__new__(type(self))
        child.__dict__.update(self.__dict__)
        if props:
            child.__dict__.update(props)
        return child

    def resolve_config(self, base: Any = None, head: Any = None) -> Any:
        """Merge intercept config from ancestors with optional base and head.

        Entries closer to the root apply first; ``base`` is prepended and
        ``head`` appended. Uses ``Config.merge`` if declared, else shallow merge.
        """
        intercept = self.ctx._intercept
        configs: list = []
        while intercept is not None and self.name in intercept:
            if intercept.has_own(self.name):
                configs.insert(0, intercept[self.name])
            intercept = intercept._parent
        if base:
            configs.insert(0, base)
        if head:
            configs.append(head)
        config_schema = getattr(type(self), "Config", None)
        merge = getattr(config_schema, "merge", None) if config_schema else None
        if callable(merge):
            return merge(*configs)
        merged: dict = {}
        for c in configs:
            if isinstance(c, dict):
                merged.update(c)
        return merged

    def __call__(self, *args: Any) -> Any:
        invoke = getattr(self, "_invoke", None)
        if invoke is None:
            raise TypeError(f"{type(self).__name__!r} service is not callable")
        return invoke(*args)


__all__ = ["Service"]
