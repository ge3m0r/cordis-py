"""Shared utilities: disposable lists, traceable proxies, and error composition.

This is a faithful Python port of ``vendor/cordis/src/utils.ts``. The traceable
machinery is adapted to Python's descriptor model: where TypeScript builds a
``Proxy`` that rebinds ``ctx`` on every property access, Python uses a small
``_Traceable`` wrapper whose attribute access delegates to the target but yields
the *caller's* context for the tracker property — the single load-bearing
behavior that lets root-shared core services attribute effects and listeners to
the correct fiber.
"""

from __future__ import annotations

import inspect
import traceback
from typing import Any, Callable, Generic, TypeVar

from .symbols import Symbol, symbols

T = TypeVar("T")

_MISSING = object()

if False:  # TYPE_CHECKING
    from .context import Context


class DisposableList(Generic[T]):
    """Ordered collection of disposable values with O(1) deletion by value.

    Mirrors the TS ``DisposableList<T extends WeakKey>``: a sequence number maps
    to each value, with a reverse index for value-keyed removal. ``push``
    returns a remover closure (the disposer's own disposer); ``clear`` returns
    the values in reverse insertion order.
    """

    def __init__(self) -> None:
        self._sn = 0
        self._map: dict[int, T] = {}
        self._rev: dict[T, int] = {}

    def __len__(self) -> int:
        return len(self._map)

    def __iter__(self):
        return iter(self._map.values())

    def push(self, value: T) -> Callable[[], bool]:
        self._sn += 1
        sn = self._sn
        self._map[sn] = value
        self._rev[value] = sn

        def remove() -> bool:
            if sn in self._map:
                del self._map[sn]
                self._rev.pop(value, None)
                return True
            return False

        return remove

    def delete(self, value: T) -> bool:
        sn = self._rev.get(value)
        if sn is None:
            return False
        del self._map[sn]
        del self._rev[value]
        return True

    def clear(self) -> list[T]:
        values = list(self._map.values())
        self._map.clear()
        self._rev.clear()
        return list(reversed(values))


class Tracker:
    """Metadata used by traceable proxies to rebind ``ctx`` and services."""

    __slots__ = ("associate", "property", "noShadow")

    def __init__(
        self,
        associate: str | None = None,
        property: str | None = None,
        noShadow: bool = False,
    ) -> None:
        self.associate = associate
        self.property = property
        self.noShadow = noShadow


def is_constructor(func: Any) -> bool:
    """Return True when a plugin callback should be constructed (a class)."""
    return inspect.isclass(func)


def is_object(value: Any) -> bool:
    """Return True for non-None objects (including callables)."""
    return value is not None and (isinstance(value, object) or callable(value))


def get_property_descriptor(target: Any, prop: str) -> Any:
    """Walk the class MRO for a descriptor/attribute, mirroring TS prototype walk."""
    for klass in type(target).__mro__:
        if prop in klass.__dict__:
            return klass.__dict__[prop]
    return None


# ---------------------------------------------------------------------------
# Traceable proxies
# ---------------------------------------------------------------------------


class _Shadow:
    """A thin view of a target where the tracker property resolves to caller ctx.

    Stand-in for the TS ``createShadow`` result: method bodies that read
    ``self.ctx`` see the caller's context instead of the service's origin one.
    """

    __slots__ = ("_target", "_ctx", "_prop")

    def __init__(self, target: Any, ctx: "Context", prop: str | None) -> None:
        object.__setattr__(self, "_target", target)
        object.__setattr__(self, "_ctx", ctx)
        object.__setattr__(self, "_prop", prop)

    def __getattr__(self, name: str) -> Any:
        if self._prop is not None and name == self._prop:
            return self._ctx
        # Bind methods to the shadow so their `self.ctx` resolves to the caller.
        raw = getattr(type(self._target), name, None)
        if raw is not None:
            return lambda *a, **k: raw(self, *a, **k)
        return getattr(self._target, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if self._prop is not None and name == self._prop:
            return  # ctx is read-only through the shadow
        setattr(self._target, name, value)


class _Traceable:
    """Proxy overlaying the caller's context onto a tracker-bearing service.

    Attribute access delegates to the target, except the tracker property
    (``ctx``) resolves to the caller's context, and callables are bound to a
    :class:`_Shadow` so their ``self.ctx`` sees the caller.
    """

    __slots__ = ("_ctx", "_target", "_tracker")

    def __init__(self, ctx: "Context", target: Any, tracker: Tracker) -> None:
        object.__setattr__(self, "_ctx", ctx)
        object.__setattr__(self, "_target", target)
        object.__setattr__(self, "_tracker", tracker)

    def __getattr__(self, name: str) -> Any:
        if name == "_origin" or name is symbols.original:
            return self._target
        if self._tracker.property is not None and name == self._tracker.property:
            return self._ctx

        inner = getattr(self._target, name, _MISSING)
        if inner is _MISSING:
            raise AttributeError(name)
        inner_tracker = getattr(inner, "_tracker", None)
        if inner_tracker is not None:
            return create_traceable(self._ctx, inner, inner_tracker)
        if callable(inner) and not self._tracker.noShadow:
            # Fetch the unbound function from the class so the shadow can be
            # bound as `self`; a bound method would double-bind the receiver.
            raw = getattr(type(self._target), name, None)
            fn = raw if raw is not None else inner
            shadow = _Shadow(self._target, self._ctx, self._tracker.property)
            return _Bound(fn, shadow)
        return inner

    def __setattr__(self, name: str, value: Any) -> None:
        if name is symbols.original:
            return
        if self._tracker.property is not None and name == self._tracker.property:
            return
        setattr(self._target, name, value)

    def __call__(self, *args: Any) -> Any:
        invoke = getattr(self._target, "_invoke", None)
        if invoke is None:
            invoke = getattr(self._target, symbols.invoke, None)
        if invoke is None:
            return self._target(*args)
        shadow = _Shadow(self._target, self._ctx, self._tracker.property)
        return invoke(shadow, *args)


class _Bound:
    """A callable bound to a shadow ``this``."""

    __slots__ = ("_fn", "_shadow")

    def __init__(self, fn: Callable, shadow: _Shadow) -> None:
        self._fn = fn
        self._shadow = shadow

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._fn(self._shadow, *args, **kwargs)


def create_traceable(ctx: "Context", value: Any, tracker: Tracker) -> Any:
    """Build a traceable proxy for ``value`` if the caller ctx is shadowed."""
    # noShadow services keep the shadow ctx (e.g. logger resolves its name from
    # the origin fiber); others strip to the prototype (origin) ctx.
    caller = ctx
    if getattr(ctx, "_has_shadow", False) and not tracker.noShadow:
        caller = ctx._parent  # type: ignore[attr-defined]
    return _Traceable(caller, value, tracker)


def get_traceable(ctx: "Context", value: T) -> T:
    """Wrap services/functions so method calls see the caller's active context."""
    if not is_object(value):
        return value
    if getattr(value, "_origin", None) is not None:
        return getattr(value, "_origin")  # type: ignore[return-value]
    tracker = getattr(value, "_tracker", None)
    if tracker is None:
        return value
    return create_traceable(ctx, value, tracker)  # type: ignore[return-value]


def with_props(target: Any, props: dict | None = None) -> Any:
    """Overlay readonly properties onto a target (TS ``withProps`` proxy)."""
    if not props:
        return target
    return _WithProps(target, props)


class _WithProps:
    __slots__ = ("_target", "_props")

    def __init__(self, target: Any, props: dict) -> None:
        object.__setattr__(self, "_target", target)
        object.__setattr__(self, "_props", props)

    def __getattr__(self, name: str) -> Any:
        if name in self._props and name != "constructor":
            return self._props[name]
        return getattr(self._target, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in self._props and name != "constructor":
            self._props[name] = value
            return
        setattr(self._target, name, value)


def create_callable(name: str, proto: Any, tracker: Tracker) -> Any:
    """Create a callable service object.

    In TypeScript this builds a function with a joined prototype so the service
    is callable through ``symbols.invoke``. Python services are callable via
    ``__call__`` (which dispatches to ``_invoke``), so this returns the proto
    unchanged after tagging its tracker.
    """
    try:
        setattr(proto, "_tracker", tracker)
    except (AttributeError, TypeError):
        pass
    return proto


# ---------------------------------------------------------------------------
# Error composition (long stack traces)
# ---------------------------------------------------------------------------


class _StackInfo:
    __slots__ = ("offset", "error")

    def __init__(self) -> None:
        self.offset = 1
        self.error = Exception()


def build_outer_stack(offset: int = 0) -> Callable[[], list[str]]:
    """Capture a lazy stack-frame supplier for later error composition."""
    outer = traceback.extract_stack(limit=6 + offset)

    def supply() -> list[str]:
        return [
            "  " + frame.format()
            for frame in outer[3 + offset :] if frame.filename
        ]

    return supply


def _augment(reason: BaseException, outer: list[str]) -> BaseException:
    note = "Cordis outer stack:\n" + "\n".join(outer) if outer else ""
    if note:
        existing = getattr(reason, "__notes__", None) or []
        if note not in existing:
            reason.__notes__ = list(existing) + [note]  # type: ignore[attr-defined]
    return reason


def compose_error(
    callback: Callable[[_StackInfo], Any],
    get_outer_stack: Callable[[], list[str]] | None = None,
) -> Any:
    """Run a callback and splice outer call-site frames into thrown errors."""
    info = _StackInfo()
    if get_outer_stack is None:
        get_outer_stack = build_outer_stack()
    try:
        result = callback(info)
    except BaseException as reason:  # noqa: BLE001
        raise _augment(reason, get_outer_stack()) from None

    if inspect.iscoroutine(result):

        async def _wrapped() -> Any:
            try:
                return await result
            except BaseException as reason:  # noqa: BLE001
                raise _augment(reason, get_outer_stack()) from None

        return _wrapped()
    return result


__all__ = [
    "DisposableList",
    "Tracker",
    "is_constructor",
    "is_object",
    "get_property_descriptor",
    "get_traceable",
    "with_props",
    "create_traceable",
    "create_callable",
    "compose_error",
    "build_outer_stack",
    "symbols",
    "Symbol",
]
