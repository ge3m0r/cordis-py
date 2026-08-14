"""Symbol keys used to avoid public property-name collisions.

Cordis (TypeScript) uses ``Symbol.for('cordis.<name>')`` so that framework-internal
slots on the context are addressable without colliding with service names. Python
has no global symbol registry; within a single process, module-level singletons
carry the same semantics (identity-unique, hashable, usable as dict keys).
"""

from __future__ import annotations


class Symbol:
    """A process-unique sentinel key.

    Equivalent in spirit to ``Symbol.for(name)``: ``Symbol.for_(name)`` returns
    the same instance for the same name across the whole process. Symbols compare
    by identity and hash by identity, so they work as dict keys.
    """

    _registry: dict[str, "Symbol"] = {}

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Symbol({self.name!r})"

    def __hash__(self) -> int:
        return id(self)

    def __eq__(self, other: object) -> bool:
        return self is other

    @classmethod
    def for_(cls, name: str) -> "Symbol":
        sym = cls._registry.get(name)
        if sym is None:
            sym = cls(name)
            cls._registry[name] = sym
        return sym


# Sentinel for "no provider" / inactive epoch, mirroring the TS string
# ``'__INACTIVE__'`` used as the inactive epoch value.
INACTIVE = "__INACTIVE__"


class _Symbols:
    """Shared symbol keys (see the TS ``symbols`` object in ``utils.ts``)."""

    # internal symbols
    shadow = Symbol.for_("cordis.shadow")
    receiver = Symbol.for_("cordis.receiver")
    original = Symbol.for_("cordis.original")
    metadata = Symbol.for_("cordis.metadata")
    initHooks = Symbol.for_("cordis.initHooks")
    checkProto = Symbol.for_("cordis.checkProto")

    # context symbols
    effect = Symbol.for_("cordis.effect")
    filter = Symbol.for_("cordis.filter")
    isolate = Symbol.for_("cordis.isolate")
    intercept = Symbol.for_("cordis.intercept")

    # service symbols
    init = Symbol.for_("cordis.init")
    check = Symbol.for_("cordis.check")
    config = Symbol.for_("cordis.config")
    invoke = Symbol.for_("cordis.invoke")
    extend = Symbol.for_("cordis.extend")
    tracker = Symbol.for_("cordis.tracker")
    resolveConfig = Symbol.for_("cordis.resolveConfig")


symbols = _Symbols

__all__ = ["Symbol", "INACTIVE", "symbols"]
