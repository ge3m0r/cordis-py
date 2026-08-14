"""Root and child dependency containers for Cordis plugins.

Faithful port of ``vendor/cordis/src/context.ts``. A context is a proxy: normal
property reads go through the service resolver (``__getattr__``), while
``extend()``, ``isolate()`` and ``intercept()`` create scoped child contexts
without mutating their parent. The single ``Context`` type carries both the
effect side (``fiber.effect``) and the coeffect side (``reflect``/``registry``)
— the unified context of Section 3.3.
"""

from __future__ import annotations

from typing import Any

from .symbols import Symbol, symbols
from .utils import Tracker, get_traceable
from .fiber import Fiber, _ProtoDict
from .reflect import ReflectService
from .registry import RegistryService
from .events import EventsService
from .logger import LoggerService

_RESERVED = {"then", "prototype"}


def _is_special(prop: str) -> bool:
    return (
        prop.startswith("_")
        or prop in _RESERVED
        or prop.isdigit()
    )


class Context:
    """Root and child dependency containers for Cordis plugins.

    A context is a proxy: normal property reads go through the service resolver,
    while ``extend()``, ``isolate()``, and ``intercept()`` create scoped child
    contexts without mutating their parent.
    """

    _is_context = True

    # Static symbol keys (mirror the TS ``Context.effect``/``filter``/...).
    effect = symbols.effect
    filter = symbols.filter
    isolate = symbols.isolate
    intercept = symbols.intercept

    @classmethod
    def is_context(cls, value: Any) -> bool:
        """Return True for Cordis context instances (cross-copy brand).

        Mirrors the TS ``Context.is(value)`` brand check (``is`` is a reserved
        word in Python, so the port names it ``is_context``).
        """
        return value is not None and getattr(value, "_is_context", False)

    def __init__(self) -> None:
        # All bootstrap fields are set through object.__setattr__ so the
        # __setattr__ trap (which needs reflect) is not triggered before the
        # services exist — mirroring how the TS constructor assigns on the raw
        # target before returning the proxy.
        object.__setattr__(self, "_parent", None)
        object.__setattr__(self, "_meta", {})
        object.__setattr__(self, "_isolate", _ProtoDict())
        object.__setattr__(self, "_intercept", _ProtoDict())
        object.__setattr__(self, "root", self)
        object.__setattr__(self, "baseUrl", None)
        object.__setattr__(self, "_filter", None)
        object.__setattr__(self, "_receiver", None)
        object.__setattr__(self, "_has_shadow", False)

        # Root fiber, then the built-in services (order matches TS).
        fiber = Fiber(self, {}, {}, None, lambda: [])
        object.__setattr__(self, "fiber", fiber)
        reflect = ReflectService(self)
        object.__setattr__(self, "reflect", reflect)
        registry = RegistryService(self)
        object.__setattr__(self, "registry", registry)
        events = EventsService(self)
        object.__setattr__(self, "events", events)
        logger = LoggerService(self)
        object.__setattr__(self, "logger", logger)
        fiber._disposables.clear()

    # ------------------------------------------------------------------
    # proxy traps
    # ------------------------------------------------------------------

    def __getattr__(self, name: str) -> Any:
        # __getattr__ runs only when normal lookup fails. Walk the parent chain
        # for inherited real fields (the TS "Reflect.has(target, prop)" branch);
        # otherwise resolve a service through reflect. Special (underscore-
        # prefixed, reserved, numeric) names are never resolved as services.
        ctx: "Context | None" = self
        while ctx is not None:
            d = ctx.__dict__
            if name in d:
                return get_traceable(self, d[name])
            ctx = d.get("_parent")
        if _is_special(name):
            raise AttributeError(name)
        return self.reflect.proxy_get(self, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if _is_special(name):
            return object.__setattr__(self, name, value)

        # find the reflect store (own or inherited)
        reflect = self.__dict__.get("reflect")
        ctx = self.__dict__.get("_parent")
        while reflect is None and ctx is not None:
            if "reflect" in ctx.__dict__:
                reflect = ctx.__dict__["reflect"]
                break
            ctx = ctx.__dict__.get("_parent")

        if reflect is None:
            # bootstrap path (no reflect yet)
            return object.__setattr__(self, name, value)

        # single set handler (mirrors the TS Proxy ``set`` trap)
        reflect.proxy_set(self, name, value)

    # ------------------------------------------------------------------
    # scoped child contexts
    # ------------------------------------------------------------------

    def extend(self, meta: dict | None = None) -> "Context":
        """Create a child context inheriting every property of this scope."""
        child = Context.__new__(Context)
        object.__setattr__(child, "_parent", self)
        object.__setattr__(child, "_meta", {})
        object.__setattr__(child, "_filter", None)
        object.__setattr__(child, "_receiver", None)
        object.__setattr__(child, "_has_shadow", False)
        if meta:
            for key, value in meta.items():
                object.__setattr__(child, key, value)
        return child

    def isolate(self, name: str, label: Symbol | None = None) -> "Context":
        """Create a child context with an independent service scope for ``name``."""
        shadow = _ProtoDict(parent=self._isolate)
        shadow[name] = label if label is not None else Symbol(name)
        child = self.extend()
        object.__setattr__(child, "_isolate", shadow)
        return child

    def intercept(self, name: str, config: Any) -> "Context":
        """Add service-specific intercept config for plugins started below here."""
        intercept = _ProtoDict(parent=self._intercept)
        intercept[name] = config
        child = self.extend()
        object.__setattr__(child, "_intercept", intercept)
        return child


__all__ = ["Context"]
