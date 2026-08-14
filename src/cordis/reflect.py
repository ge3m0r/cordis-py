"""Reflection and service-resolution layer (coeffect operations).

Faithful port of ``vendor/cordis/src/reflect.ts``. ``ReflectService`` powers the
context proxy: service registration (``provide``), the reactive notification that
drives coeffect resolution (``notify`` → ``Fiber._check_impl``/``_refresh``), and
the computed context properties (``accessor``/``mixin``) that expose core
service methods directly on ``ctx`` as reversible effects.
"""

from __future__ import annotations

import inspect
from typing import Any, Callable

from .symbols import Symbol, symbols
from .utils import Tracker, get_traceable, is_object, with_props
from .fiber import Fiber, FiberState, _ProtoDict

if False:  # TYPE_CHECKING
    from .context import Context

_MISSING = object()


class Impl:
    """Concrete service implementation record stored in the reflect store."""

    __slots__ = ("name", "fiber", "value", "check")

    def __init__(self, name: str, fiber: "Fiber", value: Any = None, check: Callable | None = None) -> None:
        self.name = name
        self.fiber = fiber
        self.value = value
        self.check = check


class Property:
    """Context property definition (service or accessor)."""

    __slots__ = ("type", "get", "set")

    def __init__(self, type: str, get: Callable | None = None, set: Callable | None = None) -> None:
        self.type = type
        self.get = get
        self.set = set


class ReflectService:
    """Reflection and service-resolution layer installed as ``ctx.reflect``."""

    _tracker: Tracker

    def __init__(self, ctx: "Context") -> None:
        self.ctx = ctx
        self._tracker = Tracker(property="ctx", noShadow=False)
        # service implementations, keyed by isolation realm symbol
        self.store: dict[Symbol, Impl] = {}
        # declared context properties (services and accessors), by name
        self.props: dict[str, Property] = {}

        self.mixin("reflect", ["get", "set", "provide", "accessor", "mixin"])
        self.mixin("fiber", ["runtime", "effect"])
        self.mixin("registry", ["inject", "plugin"])
        self.mixin(
            "events",
            ["on", "once", "parallel", "emit", "serial", "bail", "waterfall"],
        )

    # ------------------------------------------------------------------
    # proxy handler — the Context get/set/has traps delegate here
    # ------------------------------------------------------------------

    def proxy_get(self, ctx: "Context", prop: str) -> Any:
        error = Exception(f'cannot get property "{prop}" without inject')
        defn = self.props.get(prop)
        if defn is not None and defn.type == "accessor":
            return defn.get(ctx, getattr(ctx, "_receiver", None), error)  # type: ignore[misc]
        if ctx.fiber.runtime is None:
            return self.get(prop, False)
        return ctx.events.waterfall("internal/get", ctx, prop, error, lambda *a: self._resolve_chain(ctx, prop, error))

    def proxy_set(self, ctx: "Context", prop: str, value: Any) -> bool:
        error = Exception(f'cannot set property "{prop}" without provide')
        defn = self.props.get(prop)
        if defn is None:
            if ctx.fiber.runtime is None:
                # root context: undeclared sets fall through (TS root branch)
                object.__setattr__(ctx, prop, value)
                return True
            raise error
        if defn.type == "accessor":
            if defn.set is None:
                return False
            return defn.set(ctx, value, getattr(ctx, "_receiver", None), error)  # type: ignore[misc]
        return ctx.events.waterfall(
            "internal/set", ctx, prop, value, error,
            # route through the caller's ctx.reflect so the owner-fiber check
            # in `set` compares against the accessing fiber (mirrors the TS
            # traceable rebind), not the root reflect's fiber.
            lambda *a: ctx.reflect.set(prop, value, error),
        )

    def proxy_has(self, ctx: "Context", prop: str) -> bool:
        return self.props.get(prop) is not None

    def _resolve_chain(self, ctx: "Context", prop: str, error: Exception) -> Any:
        key = ctx._isolate.get(prop)
        fiber = ctx.fiber
        while True:
            impl = fiber.store.get(prop) if fiber.store else None
            if impl:
                return get_traceable(ctx, impl.value)
            if prop in fiber.inject:
                error.args = (f'cannot get required service "{prop}" in inactive context',)
                raise error
            if not fiber.runtime:
                raise error
            parent_key = fiber.parent._isolate.get(prop) if fiber.parent else _MISSING
            if parent_key is not key:
                raise error
            fiber = fiber.parent.fiber

    # ------------------------------------------------------------------
    # service store access
    # ------------------------------------------------------------------

    def get(self, name: str, strict: bool = True) -> Any:
        impl = self._get_impl(name, strict)
        return get_traceable(self.ctx, impl.value if impl else None)

    def _get_impl(self, name: str, strict: bool = True) -> Impl | None:
        key = self.ctx._isolate.get(name)
        if key is None:
            return None
        impl = self.store.get(key)
        if not impl:
            return None
        if strict and impl.fiber.state != FiberState.ACTIVE:
            return None
        return impl

    def set(self, name: str, value: Any, error: Exception | None = None) -> bool:
        key = self.ctx._isolate[name]
        impl = self.store.get(key)
        if not impl:
            raise Exception(f'cannot set property "{name}" without provide')
        if impl.fiber is not self.ctx.fiber:
            raise Exception(f'cannot set property "{name}" in multiple fibers')
        impl.value = value
        return True

    def provide(self, name: str, value: Any = None, check: Callable | None = None) -> Callable:
        return self.ctx.fiber.effect(
            lambda: self._provide_body(name, value, check),
            f"ctx.provide({name!r})",
        )

    def _provide_body(self, name: str, value: Any, check: Callable | None) -> Callable:
        existing = self.props.get(name)
        if existing is None:
            self.props[name] = Property("service")
        elif existing.type != "service":
            raise Exception(f'property "{name}" is already declared as {existing.type}')
        self.props[name] = Property("service")

        root = self.ctx.root
        root._isolate._own.setdefault(name, Symbol(name))
        key = self.ctx._isolate[name]
        if key in self.store:
            raise Exception(f'service "{name}" has been registered at <{self.store[key].fiber.name}>')
        impl = Impl(name, self.ctx.fiber, value, check)
        self.store[key] = impl
        self.ctx.fiber.store[name] = impl  # type: ignore[index]
        if self.ctx.fiber.state == FiberState.ACTIVE:
            self.notify([name])

        async def inverse() -> None:
            del self.store[key]
            fibers = self.notify([name])
            if fibers:
                await __import__("asyncio").gather(
                    *(f.await_() for f in fibers), return_exceptions=True
                )
            if self.ctx.fiber.store is not None:
                self.ctx.fiber.store.pop(name, None)  # type: ignore[union-attr]

        return inverse

    def notify(
        self,
        names: list[str],
        filter: Callable[["Context", str], bool] | None = None,
    ) -> list["Fiber"]:
        """Re-evaluate every fiber that requires one of the given services.

        This is Definition 26's reactive classification: each dependent fiber
        has its injected impls rechecked (``_check_impl``) and its epoch
        recomputed (``_refresh``), starting an activating or deactivating
        transition via ``_set_epoch``.
        """
        if filter is None:
            reflect_ctx = self.ctx
            filter = lambda ctx, name: ctx._isolate.get(name, _MISSING) is reflect_ctx._isolate.get(name, _MISSING)

        fibers: list[Fiber] = []
        for runtime in self.ctx.registry.values():
            for fiber in runtime.fibers:
                has_update = False
                for name in names:
                    if name not in fiber.inject:
                        continue
                    if not filter(fiber.ctx, name):
                        continue
                    has_update = True
                    fiber._check_impl(name)
                if not has_update:
                    continue
                fiber._refresh()
                fibers.append(fiber)

        for name in names:
            child = self.ctx.extend()
            child._filter = lambda target, _name=name: filter(target, _name)  # type: ignore[attr-defined]
            impl = self._get_impl(name, False)
            self.ctx.events.emit(child, "internal/service", name, impl.value if impl else None)
        return fibers

    # ------------------------------------------------------------------
    # computed context properties
    # ------------------------------------------------------------------

    def accessor(self, name: str, options: dict) -> Callable:
        get = options["get"]
        set = options.get("set")

        def body() -> Callable:
            if name in self.props:
                raise Exception(f'property "{name}" is already declared as {self.props[name].type}')
            self.props[name] = Property("accessor", get, set)
            return lambda: self.props.pop(name, None)

        return self.ctx.fiber.effect(body, f"ctx.accessor({name!r})")

    def mixin(self, source: Any, mixins: Any) -> Any:
        reflect = self

        def body():
            if isinstance(mixins, (list, tuple)):
                entries = [(k, k) for k in mixins]
            else:
                entries = list(mixins.items())

            for key, value in entries:
                yield reflect.accessor(
                    value,
                    {
                        "get": _make_mixin_get(reflect, source, key),
                        "set": _make_mixin_set(reflect, source, key),
                    },
                )

        return self.ctx.fiber.effect(body, f"ctx.mixin({source!r})")

    # ------------------------------------------------------------------
    # tracing
    # ------------------------------------------------------------------

    def trace(self, value: Any) -> Any:
        return get_traceable(self.ctx, value)

    def bind(self, callback: Callable) -> Callable:
        """Wrap a callback so calls trace ``this`` and arguments to this context.

        The TypeScript version builds a Proxy that rebinds every call; the
        scope-attribution it provides is preserved best-effort here. The core
        mechanisms (listener ownership by fiber, context filtering) do not
        depend on call-time tracing.
        """
        reflect = self

        def wrapper(*args: Any) -> Any:
            traced = [reflect.trace(a) for a in args]
            return callback(*traced)

        return wrapper


def _make_mixin_get(reflect: "ReflectService", source: Any, key: str) -> Callable:
    def get(this: "Context", receiver: Any, error: Exception) -> Any:
        service = getattr(this, source) if isinstance(source, str) else source
        if service is None:
            return service
        # Fetch the unbound function from the class so we can rebind `this`
        # to the overlay (a bound method would double-bind the receiver).
        raw = getattr(type(service), key, None)
        if raw is not None:
            mixin = with_props(receiver, service) if receiver else service
            return lambda *a, **k: raw(mixin, *a, **k)
        # ``service`` is a traceable (or an instance attribute): its attribute
        # already binds to the caller context, so return it verbatim — wrapping
        # it again would prepend an extra receiver argument.
        return getattr(service, key)

    return get


def _make_mixin_set(reflect: "ReflectService", source: Any, key: str) -> Callable:
    def set(this: "Context", value: Any, receiver: Any, error: Exception) -> bool:
        service = getattr(this, source) if isinstance(source, str) else source
        mixin = with_props(receiver, service) if receiver else service
        target = mixin if receiver else service
        setattr(target, key, value)
        return True

    return set


__all__ = ["ReflectService", "Impl", "Property"]
