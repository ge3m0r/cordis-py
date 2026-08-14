"""Typed event bus: emit / parallel / serial / bail / waterfall.

Faithful port of ``vendor/cordis/src/events.ts``. The dispatch-mode split is
preserved exactly: ``emit``, ``bail`` and ``waterfall`` are synchronous (so
``ctx.tools``-style service reads, which run through the synchronous
``internal/get`` waterfall, stay synchronous — matching TypeScript); ``parallel``
and ``serial`` are coroutines that await their listeners.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, Awaitable, Callable

from .utils import Tracker, compose_error, symbols

if False:  # TYPE_CHECKING
    from .context import Context
    from .fiber import Fiber, FiberState


class AggregateError(Exception):
    """Aggregate of listener failures raised by ``parallel`` dispatch."""

    def __init__(self, errors: list[BaseException]) -> None:
        self.errors = errors
        super().__init__(f"{len(errors)} listener(s) failed")


def is_bailed(value: Any) -> bool:
    """Return whether a result should stop a bail-style dispatch."""
    return value is not None and value is not False


DispatchMode = str  # 'emit' | 'parallel' | 'serial' | 'bail' | 'waterfall'


class EventOptions:
    __slots__ = ("prepend", "global_")

    def __init__(self, prepend: bool = False, global_: bool = False) -> None:
        self.prepend = prepend
        self.global_ = global_


class Hook:
    __slots__ = ("ctx", "callback", "prepend", "global_")

    def __init__(self, ctx: "Context", callback: Callable, options: EventOptions) -> None:
        self.ctx = ctx
        self.callback = callback
        self.prepend = options.prepend
        self.global_ = options.global_


async def _run_listener(cb: Callable, args: list) -> Any:
    result = cb(*args)
    if inspect.iscoroutine(result):
        return await result
    return result


class EventsService:
    """Event bus installed as ``ctx.events`` and mixed into every context."""

    _tracker: Tracker

    def __init__(self, ctx: "Context") -> None:
        self.ctx = ctx
        self._tracker = Tracker(property="ctx", noShadow=False)
        self._hooks: dict[Any, list[Hook]] = {}

        # internal/listener interceptor: route internal/update registrations
        # onto the calling fiber's own _hooks so update/HMR hooks unload with
        # the fiber that declared them.
        self.on(
            "internal/listener",
            _internal_listener,
            EventOptions(prepend=True, global_=True),
        )
        # internal/update waterfall dispatcher (global, prepended) — runs the
        # fiber-local update hooks in order, finally delegating to next.
        self.on(
            "internal/update",
            _internal_update_dispatch,
            EventOptions(prepend=True, global_=True),
        )

    # ------------------------------------------------------------------
    # dispatch
    # ------------------------------------------------------------------

    def dispatch(self, type: str, args: list) -> tuple[list[Callable], Any, list]:
        """Resolve listeners for one dispatch and apply context filtering.

        Returns ``(callbacks, this_arg, event_args)``. In TypeScript the
        dispatch context is bound as the listener's ``this``; Python has no
        implicit ``this``, so the mode methods render it as a leading argument
        when present (the per-event contract decides whether one is passed).
        """
        this_arg: Any = None
        if args and (
            getattr(args[0], "_is_context", False) or getattr(args[0], "_is_fiber", False)
        ):
            this_arg = args.pop(0)
        name = args.pop(0)
        if not (isinstance(name, str) and name.startswith("internal/")):
            self.emit("internal/dispatch", type, name, list(args), this_arg)
        filter_ = getattr(this_arg, "_filter", None) if this_arg is not None else None
        hooks = self._hooks.get(name, [])
        out: list[Callable] = []
        for hook in hooks:
            if hook.global_ or not filter_ or filter_(hook.ctx):
                out.append(hook.callback)
        return out, this_arg, args

    async def parallel(self, *args: Any) -> None:
        """Run listeners concurrently and wait for all of them."""
        cbs, this_arg, event_args = self.dispatch("emit", list(args))
        if not cbs:
            return
        call_args = [this_arg, *event_args] if this_arg is not None else event_args
        results = await asyncio.gather(
            *(_run_listener(cb, call_args) for cb in cbs), return_exceptions=True
        )
        errors = [r for r in results if isinstance(r, BaseException)]
        if errors:
            raise AggregateError(errors)

    def emit(self, *args: Any) -> None:
        """Run listeners synchronously without waiting for returned coroutines."""
        cbs, this_arg, event_args = self.dispatch("emit", list(args))
        call_args = [this_arg, *event_args] if this_arg is not None else event_args
        for cb in cbs:
            result = cb(*call_args)
            if inspect.iscoroutine(result):
                _schedule_coro(self.ctx, result)

    async def serial(self, *args: Any) -> Any:
        """Run listeners in order, awaiting each, until one bails."""
        cbs, this_arg, event_args = self.dispatch("serial", list(args))
        call_args = [this_arg, *event_args] if this_arg is not None else event_args
        for cb in cbs:
            result = await _run_listener(cb, call_args)
            if is_bailed(result):
                return result
        return None

    def bail(self, *args: Any) -> Any:
        """Run listeners synchronously until one returns a bail value."""
        cbs, this_arg, event_args = self.dispatch("bail", list(args))
        call_args = [this_arg, *event_args] if this_arg is not None else event_args
        for cb in cbs:
            result = cb(*call_args)
            if is_bailed(result):
                return result
        return None

    def waterfall(self, *args: Any) -> Any:
        """Compose listeners around the final ``next`` callback.

        The last argument is the innermost ``next``; listeners wrap the rest of
        the chain. A listener that does not call ``next()`` vetoes the chain.
        """
        cbs, this_arg, event_args = self.dispatch("waterfall", list(args))
        inner = event_args[-1]
        rest = list(event_args[:-1])
        if this_arg is not None:
            rest = [this_arg, *rest]

        def next_cb() -> Any:
            if cbs:
                cb = cbs.pop(0)
                return cb(*rest, next_cb)
            # innermost default: takes no args (TS ``() => ...`` ignores extras)
            return inner()

        return next_cb()

    # ------------------------------------------------------------------
    # registration
    # ------------------------------------------------------------------

    def register(
        self,
        label: str,
        hooks: list[Hook],
        callback: Callable,
        options: EventOptions,
    ) -> Callable[[], Any]:
        def body() -> Callable[[], bool]:
            hook = Hook(self.ctx, callback, options)
            if options.prepend:
                hooks.insert(0, hook)
            else:
                hooks.append(hook)
            return lambda: self.unregister(hooks, callback)

        return self.ctx.fiber.effect(body, label)

    def unregister(self, hooks: list[Hook], callback: Callable) -> bool:
        for i, hook in enumerate(hooks):
            if hook.callback is callback:
                hooks.pop(i)
                return True
        return False

    def on(
        self,
        name: str | Any,
        listener: Callable,
        options: bool | EventOptions | None = None,
    ) -> Callable[[], bool]:
        if isinstance(options, bool):
            options = EventOptions(prepend=options)
        elif options is None:
            options = EventOptions()

        self.ctx.fiber.assert_active()
        listener = self.ctx.reflect.bind(listener)
        result = self.bail(self.ctx, "internal/listener", name, listener, options)
        if result:
            return result  # type: ignore[return-value]

        hooks = self._hooks.setdefault(name, [])
        label = f"ctx.on({name!r})"
        return self.register(label, hooks, listener, options)

    def once(
        self,
        name: str | Any,
        listener: Callable,
        options: bool | EventOptions | None = None,
    ) -> Callable[[], bool]:
        dispose: Callable[[], bool] | None = None

        def wrapper(*args: Any) -> Any:
            assert dispose is not None
            dispose()
            return listener(*args)

        dispose = self.on(name, wrapper, options)
        return dispose


def _schedule_coro(ctx: "Context", coro: Any) -> None:
    """Fire-and-forget a coroutine emitted from a synchronous ``emit``."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        return

    task = loop.create_task(coro)

    def _done(t: asyncio.Task) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            try:
                ctx.logger.error(exc)
            except Exception:
                pass

    task.add_done_callback(_done)


def _internal_update_dispatch(
    this: Any, config: Any, no_save: bool, next_cb: Callable
) -> Any:
    """Dispatch the fiber-local ``internal/update`` hooks, then ``next``.

    ``this`` is the dispatching fiber (TS ``this: Fiber``); each hook receives
    ``(fiber, config, no_save, next)`` so it may veto by not calling ``next``.
    """
    hooks = list(getattr(this, "_hooks", {}).get("internal/update", []))

    def run() -> Any:
        if hooks:
            cb = hooks.pop(0)
            return cb(this, config, no_save, run)
        # no more fiber-local hooks: resume the waterfall's inner default,
        # which takes no args (TS ``() => ...`` ignores the call args).
        return next_cb()

    return run()


def _internal_listener(
    this: "Context",
    name: str | Any,
    listener: Callable,
    options: EventOptions,
) -> Any:
    """Route non-global ``internal/update`` listeners onto the calling fiber."""
    if name == "internal/update" and not options.global_:
        hooks = this.fiber._hooks.setdefault("internal/update", [])
        if options.prepend:
            hooks.insert(0, listener)
        else:
            hooks.append(listener)
        return True  # bail: replace default registration
    return None


# Built-in framework events (mirrors the TS ``Events`` interface).
# These are string contracts; the dispatch mode is part of each event's public
# contract and is documented with an @mode tag in the generated catalog.
INTERNAL_EVENTS = (
    "internal/plugin",
    "internal/status",
    "internal/config",
    "internal/service",
    "internal/update",
    "internal/get",
    "internal/set",
    "internal/listener",
    "internal/dispatch",
)


__all__ = [
    "EventsService",
    "Hook",
    "EventOptions",
    "AggregateError",
    "is_bailed",
    "DispatchMode",
]
