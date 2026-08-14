"""Plugin registry: normalizes plugin shapes, starts fibers, tracks runtimes.

Faithful port of ``vendor/cordis/src/registry.ts``. ``RegistryService`` is
installed as ``ctx.registry`` and mixed into every context as ``ctx.plugin`` /
``ctx.inject``. A plugin may be a function, a class, or an ``{apply}`` object;
each carries optional ``name``, ``Config`` (schema), ``inject`` (coeffect
specification), ``provide`` (provision), and ``intercept`` metadata.
"""

from __future__ import annotations

import inspect
from typing import Any, Callable

from .symbols import symbols
from .utils import DisposableList, Tracker, build_outer_stack
from .fiber import Fiber

if False:  # TYPE_CHECKING
    from .context import Context


def is_applicable(obj: Any) -> bool:
    return (
        obj is not None
        and isinstance(obj, object)
        and callable(getattr(obj, "apply", None))
    )


class Inject:
    """Service dependency declaration and the ``@Inject`` decorator."""

    @staticmethod
    def resolve(inject: Any, result: dict | None = None) -> dict:
        """Normalize an inject declaration into a plain name → config map."""
        if result is None:
            result = {}
        if not inject:
            return result
        if isinstance(inject, (list, tuple)):
            for name in inject:
                result[name] = None
        elif getattr(inject, "_check_proto", False):
            # class-inherited inject metadata: merge prototype then own keys
            proto = getattr(inject, "_proto", None)
            if proto is not None:
                Inject.resolve(proto, result)
            for name in dict(inject):
                if name == "_check_proto" or name == "_proto":
                    continue
                result[name] = inject[name] if inject[name] is not None else None
        elif isinstance(inject, dict):
            for name in inject:
                result[name] = inject[name] if inject[name] is not None else None
        return result

    def __call__(self, value: Any) -> Any:
        """Class decorator: declare a service dependency on a class."""
        if inspect.isclass(value):
            if "inject" not in value.__dict__ or value.__dict__.get("inject") is None:
                value.inject = {}
                setattr(value.inject, "_check_proto", True)
                proto = getattr(getattr(type(value), "inject", None), "_proto", None)
                if proto is not None:
                    setattr(value.inject, "_proto", proto)
                else:
                    setattr(value.inject, "_proto", None)
            else:
                if not getattr(value.inject, "_check_proto", False):
                    base = dict(value.inject)
                    value.inject = base
                    setattr(value.inject, "_check_proto", True)
                    setattr(value.inject, "_proto", getattr(type(value), "inject", None))
            value.inject[self._name] = self._config
            return value
        raise TypeError("@Inject() can only be used on classes (method form is simplified)")

    def __init__(self, name: str, config: Any = None) -> None:
        self._name = name
        self._config = config


class Plugin:
    """Supported plugin entrypoint shapes and the shared runtime record."""

    class Runtime:
        """Mutable registry record shared by all fibers of one plugin callback."""

        __slots__ = ("name", "fibers", "callback", "Config")

        def __init__(self, name: str | None, callback: Callable, fibers: DisposableList, Config: Any = None) -> None:
            self.name = name
            self.callback = callback
            self.fibers = fibers
            self.Config = Config


class RegistryService:
    """Plugin registry installed as ``ctx.registry``."""

    _tracker: Tracker

    def __init__(self, ctx: "Context") -> None:
        self.ctx = ctx
        self._tracker = Tracker(property="ctx", noShadow=True)
        self._counter = 0
        self._internal: dict[Callable, Plugin.Runtime] = {}

    @property
    def counter(self) -> int:
        self._counter += 1
        return self._counter

    @property
    def size(self) -> int:
        return len(self._internal)

    def resolve(self, plugin: Any) -> Callable | None:
        try:
            if callable(plugin):
                return plugin
            if is_applicable(plugin):
                return plugin.apply
        except Exception:  # noqa: BLE001
            pass
        return None

    def get(self, plugin: Any) -> Plugin.Runtime | None:
        key = self.resolve(plugin)
        return self._internal.get(key) if key else None

    def has(self, plugin: Any) -> bool:
        key = self.resolve(plugin)
        return bool(key and key in self._internal)

    def delete(self, plugin: Any) -> Plugin.Runtime | None:
        key = self.resolve(plugin)
        runtime = self._internal.get(key) if key else None
        if runtime is None:
            return None
        del self._internal[key]
        for fiber in list(runtime.fibers):
            fiber.dispose()
        return runtime

    def keys(self):
        return self._internal.keys()

    def values(self):
        return self._internal.values()

    def entries(self):
        return self._internal.items()

    def for_each(self, callback: Callable) -> None:
        for key, runtime in self._internal.items():
            callback(runtime, key)

    def inject(self, inject: Any, callback: Callable) -> "Fiber":
        return self.plugin(
            type("Plugin", (), {"apply": staticmethod(callback), "inject": inject, "name": getattr(callback, "__name__", None)})(),
        )

    def plugin(self, plugin: Any, config: Any = None, get_outer_stack: Callable | None = None) -> "Fiber":
        if get_outer_stack is None:
            get_outer_stack = build_outer_stack()
        callback = self.resolve(plugin)
        if not callback:
            raise TypeError(
                'invalid plugin, expect function or object with an "apply" method, received '
                + type(plugin).__name__
            )
        self.ctx.fiber.assert_active()

        runtime = self._internal.get(callback)
        if runtime is None:
            name = getattr(plugin, "name", None)
            if name == "apply":
                name = None
            runtime = Plugin.Runtime(
                name=name,
                callback=callback,
                fibers=DisposableList(),
                Config=getattr(plugin, "Config", None),
            )
            self._internal[callback] = runtime

        inject_map = Inject.resolve(getattr(plugin, "inject", None))
        fiber = Fiber(self.ctx, config, inject_map, runtime, get_outer_stack)
        return fiber


__all__ = ["RegistryService", "Plugin", "Inject"]
