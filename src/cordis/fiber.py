"""Component lifecycle and revertible effects.

Faithful port of ``vendor/cordis/src/fiber.ts``. A ``Fiber`` is the runtime
instance of one plugin application (the paper's *component* / *fiber*). It
tracks:

* **revertible effects** — ``ctx.effect(execute)`` runs ``execute`` immediately
  and collects the disposers (inverses) it returns; they run in reverse order
  when the effect or its owning fiber is disposed (the LIFO accumulator of
  Theorem 61 / Corollary 62).
* **reactive coeffects** — ``inject`` is the specification; ``_refresh`` rebuilds
  the epoch (a digest of providing-fiber uids) and ``_set_epoch`` starts a
  ``_reload`` (activating) or ``_unload`` (deactivating) accordingly. This is
  Definition 26's activating/deactivating/neutral classification.
* **lifecycle** — the ``FiberState`` machine mirrors the calculus transitions
  (L-Begin/Iter/Finish/Divert/Raise/Leave/Unload).
"""

from __future__ import annotations

import asyncio
import inspect
import weakref
from enum import IntEnum
from typing import Any, Awaitable, Callable

from .symbols import INACTIVE, symbols
from .utils import (
    DisposableList,
    compose_error,
    is_constructor,
    is_object,
    build_outer_stack,
)
from .events import _schedule_coro

if False:  # TYPE_CHECKING
    from .context import Context
    from .registry import Plugin


class ValidationError(TypeError):
    """Raised when plugin configuration fails schema validation."""

    def __init__(self, issues: Any) -> None:
        self.issues = issues
        super().__init__("invalid config:\n" + _format_issues(issues))


def _format_issues(issues: Any) -> str:
    if not issues:
        return ""
    out = []
    for issue in issues:
        path = issue.get("path") if isinstance(issue, dict) else None
        msg = issue.get("message") if isinstance(issue, dict) else str(issue)
        if path:
            out.append(f"  - {msg} (at {'.'.join(str(p) for p in path)})")
        else:
            out.append(f"  - {msg}")
    return "\n".join(out)


def resolve_config(runtime: "Plugin.Runtime", config: Any) -> Any:
    """Validate and normalize config for a plugin runtime before it starts."""
    schema = getattr(runtime, "Config", None)
    if not schema:
        return config
    # Standard-schema-like: validate() -> (value, issues) or raises
    validate = getattr(schema, "validate", None)
    if callable(validate):
        result = validate(config)
        if isinstance(result, tuple) and len(result) == 2:
            value, issues = result
            if issues:
                raise ValidationError(issues)
            return value
        return result
    if callable(schema):
        return schema(config)
    return config


# ---------------------------------------------------------------------------
# Effect types
# ---------------------------------------------------------------------------

Disposable = Callable[[], Any]


class EffectMeta:
    __slots__ = ("label", "children")

    def __init__(self, label: str, children: list["EffectMeta"] | None = None) -> None:
        self.label = label
        self.children = children or []


class _EffectRunner:
    __slots__ = ("epoch", "execute", "collect", "get_outer_stack")

    def __init__(self, epoch: Any, execute: Callable, collect: Callable, get_outer_stack: Callable) -> None:
        self.epoch = epoch
        self.execute = execute
        self.collect = collect
        self.get_outer_stack = get_outer_stack


# Public effect disposers are single-shot, but structural owners and outer
# effects must still be able to join a cleanup that another caller started.
# Keyed weakly by the AsyncDisposable so the entry is reclaimed once the
# disposer is GC'd (mirrors the TS ``new WeakMap<Disposable, ...>``).
effect_inertia: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def run_disposable(dispose: Disposable) -> Any:
    result = dispose()
    try:
        task = effect_inertia.get(dispose)
    except TypeError:
        # non-weakref-able disposers (e.g. bound methods) have no inertia entry
        task = None
    if task is not None and inspect.isawaitable(task):
        return task
    return result


def emit_plugin_disposed(context: "Context", fiber: "Fiber") -> None:
    """Notify ``internal/plugin`` teardown without letting one observer break cleanup."""
    args: list = ["internal/plugin", fiber]
    try:
        callbacks, this_arg, event_args = context.events.dispatch("emit", list(args))
    except Exception as error:  # noqa: BLE001
        context.logger.error(error)
        return
    call_args = [this_arg, *event_args] if this_arg is not None else event_args
    for cb in callbacks:
        try:
            result = cb(*call_args)
            if inspect.iscoroutine(result):
                _schedule_coro(context, result)
        except Exception as error:  # noqa: BLE001
            context.logger.error(error)


class FiberState(IntEnum):
    PENDING = 0
    LOADING = 1
    ACTIVE = 2
    FAILED = 3
    DISPOSED = 4
    UNLOADING = 5


class CordisError(Exception):
    """Framework error with a stable machine-readable code."""

    CODES = {
        "INACTIVE_EFFECT": "cannot create effect on inactive context",
    }

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or self.CODES.get(code, code))


def _create_task(coro: Any) -> asyncio.Task:
    """Schedule a coroutine as a task on the running loop (Cordis is async-native)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError as exc:
        raise CordisError(
            "INACTIVE_EFFECT",
            "Cordis requires a running asyncio event loop for lifecycle transitions",
        ) from exc
    return loop.create_task(coro)


# ---------------------------------------------------------------------------
# AsyncDisposable — the disposer returned by ctx.effect()
# ---------------------------------------------------------------------------


class AsyncDisposable:
    """Disposer returned by :meth:`Fiber.effect`.

    Callable (``dispose()``) and awaitable (``await effect`` waits for setup to
    finish, then yields the dispose closure). Disposers run the collected
    inverses in reverse order, awaiting async ones; calling twice is a no-op.
    """

    __slots__ = (
        "_runner",
        "_execute_task",
        "_disposables",
        "_disposing",
        "_in_flight",
        "_effect_meta",
        "_remove",
        "__weakref__",
    )

    def __init__(
        self,
        runner: _EffectRunner,
        execute_task: asyncio.Task | None,
        disposables: list,
        effect_meta: EffectMeta,
        remove: Callable[[], bool] | None = None,
    ) -> None:
        self._runner = runner
        self._execute_task = execute_task
        self._disposables = disposables  # the effect's local inverse list
        self._disposing = False
        self._in_flight: asyncio.Task | None = None
        self._effect_meta = effect_meta
        # Remover that unlinks this wrapper from the owning fiber's
        # ``_disposables`` (mirrors the TS ``removeWrapper`` / finalizeDisposal).
        self._remove = remove

    def __call__(self) -> Any:
        return self._dispose()

    def _dispose(self) -> Any:
        if self._disposing:
            return self._in_flight
        self._disposing = True
        self._runner.epoch = False  # halt async-gen effect production

        # Capture state as locals so the async task closures below do NOT capture
        # `self`; otherwise the running task would keep the AsyncDisposable alive
        # and the weak-keyed inertia entry could never be reclaimed (a leak).
        execute_task = self._execute_task
        disposables = self._disposables
        remove = self._remove  # unlink from the owning fiber's _disposables

        async def run_inverses(items: list) -> None:
            for d in items:
                res = run_disposable(d)
                if inspect.isawaitable(res):
                    await res

        # async setup still running: wait for it, then run all inverses async.
        if execute_task is not None and not execute_task.done():
            async def _do():
                try:
                    await asyncio.shield(execute_task)
                except BaseException:
                    pass
                try:
                    items = list(reversed(disposables))
                    disposables.clear()
                    await run_inverses(items)
                finally:
                    if remove is not None:
                        remove()

            task = _create_task(_do())
            self._in_flight = task
            effect_inertia[self] = task
            return task

        # sync path: run inverses in reverse; the moment one yields a coroutine,
        # switch to async for the remainder. A fully-sync effect (the common
        # case for ``ctx.on`` / ``ctx.provide`` removal) disposes synchronously
        # so a later synchronous dispatch does not observe the removed effect.
        items = list(reversed(disposables))
        disposables.clear()
        for i, d in enumerate(items):
            res = run_disposable(d)
            if inspect.isawaitable(res):
                rest = items[i + 1 :]
                pending = res

                async def _do_rest(_pending=pending, _rest=rest):
                    try:
                        await _pending
                        await run_inverses(_rest)
                    finally:
                        if remove is not None:
                            remove()

                task = _create_task(_do_rest())
                self._in_flight = task
                effect_inertia[self] = task
                return task
        # fully synchronous disposal — unlink now (TS finalizeDisposal removeWrapper)
        if remove is not None:
            remove()
        return None

    def __await__(self):
        return self._await().__await__()

    async def _await(self) -> Any:
        if self._execute_task is not None:
            await self._execute_task
        return self._dispose


# ---------------------------------------------------------------------------
# Fiber
# ---------------------------------------------------------------------------


class Fiber:
    """Runtime instance of one plugin application."""

    _is_fiber = True

    def __init__(
        self,
        parent: "Context",
        config: Any,
        inject: dict,
        runtime: "Plugin.Runtime | None",
        get_outer_stack: Callable[[], list[str]],
    ) -> None:
        self.parent = parent
        self._config = config
        self.inject = inject
        self.runtime = runtime
        self._disposables = DisposableList()
        self._hooks: dict[str, list[Callable]] = {}
        self.uid: int | None = None
        self.store: dict | None = None
        self.inertia: asyncio.Task | None = None
        self.state = FiberState.PENDING
        self._error: Any = None
        self._store: dict[str, Any] = {}

        collect = lambda dispose: self._disposables.push(dispose)

        if runtime is not None:
            self.uid = parent.registry.counter
            self.ctx = parent.extend({"fiber": self})

            if inject:
                self.ctx._intercept = _ProtoDict(parent=parent._intercept)
                for name, cfg in inject.items():
                    if cfg is None:
                        continue
                    self.ctx._intercept[name] = cfg

            fiber_self = self

            def execute() -> Any:
                if is_constructor(runtime.callback):
                    instance = runtime.callback(fiber_self.ctx, fiber_self.config)
                    for hook in getattr(instance, "_init_hooks", None) or []:
                        hook()
                    init = getattr(instance, "_init", None)
                    return init() if init is not None else None
                else:
                    return runtime.callback(fiber_self.ctx, fiber_self.config)

            self._runner = _EffectRunner(
                epoch=INACTIVE,
                execute=execute,
                collect=collect,
                get_outer_stack=get_outer_stack,
            )

            def dispose_body() -> Callable:
                remove = runtime.fibers.push(self)

                async def inverse() -> None:
                    self.uid = None
                    emit_plugin_disposed(self.ctx, self)
                    if self.ctx.registry.has(runtime.callback):
                        remove()
                        if not len(runtime.fibers):
                            self.ctx.registry.delete(runtime.callback)
                    self._set_epoch(INACTIVE)
                    if not self.inertia:
                        def cb() -> FiberState:
                            self.inertia = _create_task(self._unload())
                            return FiberState.UNLOADING

                        self._update_state(cb)
                    while self.inertia:
                        await self.inertia

                return inverse

            self.dispose = parent.fiber.effect(dispose_body, "ctx.plugin()")

            try:
                self.ctx.emit("internal/plugin", self)
            except Exception as error:  # noqa: BLE001
                async def _cleanup() -> None:
                    await self.dispose()

                _schedule_coro(self.ctx, _cleanup())
                raise

            if self.uid is not None and parent.fiber.state != FiberState.UNLOADING:
                for name in list(self.inject):
                    self._check_impl(name)
                self._refresh()
        else:
            # root fiber
            self.uid = 0
            self.ctx = parent
            self.state = FiberState.ACTIVE
            self.store = {}

            def noop() -> None:
                return None

            self._runner = _EffectRunner(
                epoch="",
                execute=noop,
                collect=collect,
                get_outer_stack=get_outer_stack,
            )
            self.dispose = self.restart  # type: ignore[assignment]

    # ------------------------------------------------------------------
    # diagnostics
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        fiber: Fiber = self
        while True:
            if fiber.runtime and fiber.runtime.name:
                return fiber.runtime.name
            if fiber.parent is None or fiber.parent.fiber is fiber:
                break
            fiber = fiber.parent.fiber
        return "root"

    def assert_active(self) -> None:
        if self.uid is None:
            raise CordisError("INACTIVE_EFFECT")

    # ------------------------------------------------------------------
    # effect execution
    # ------------------------------------------------------------------

    def _execute(self, runner: _EffectRunner) -> Any:
        old_epoch = runner.epoch

        def safe_collect(dispose: Any) -> None:
            if callable(dispose):
                runner.collect(dispose)
            elif dispose is not None:
                raise TypeError("Invalid effect")

        effect = runner.execute()
        if callable(effect) and not inspect.iscoroutine(effect) and not inspect.isasyncgen(effect):
            runner.collect(effect)
            return None
        if effect is None:
            return None
        if inspect.iscoroutine(effect):
            async def _await_effect() -> None:
                res = await effect
                safe_collect(res)

            return _await_effect()
        if inspect.isgenerator(effect):
            for val in effect:
                safe_collect(val)
            return None
        if inspect.isasyncgen(effect):
            async def _iter_async() -> None:
                while True:
                    if runner.epoch != old_epoch:
                        return
                    try:
                        val = await effect.__anext__()
                    except StopAsyncIteration:
                        return
                    safe_collect(val)

            return _iter_async()
        if is_object(effect):
            raise TypeError("Invalid effect")
        raise TypeError("Invalid effect")

    # ------------------------------------------------------------------
    # ctx.effect — revertible effects
    # ------------------------------------------------------------------

    def effect(self, execute: Callable, label: str = "anonymous") -> AsyncDisposable:
        self.assert_active()
        if self.state == FiberState.UNLOADING:
            raise CordisError("INACTIVE_EFFECT")

        disposables: list[Disposable] = []
        meta = EffectMeta(label)

        def collect(dispose: Disposable) -> None:
            disposables.append(dispose)
            self._disposables.delete(dispose)
            child = getattr(dispose, "_effect_meta", None)
            if child is not None:
                meta.children.append(child)

        runner = _EffectRunner(
            epoch=True,
            execute=execute,
            collect=collect,
            get_outer_stack=build_outer_stack(),
        )

        ad = AsyncDisposable(runner, None, disposables, meta)
        ad._effect_meta = meta  # type: ignore[attr-defined]

        # register the wrapper on self._disposables so a parent unload sees it.
        # The inertia entry is weak-keyed by `ad`; _dispose fills in the task.
        # Capture the remover so _dispose can unlink the wrapper from this
        # fiber's list once disposal completes (mirrors TS finalizeDisposal).
        remove = self._disposables.push(ad)
        ad._remove = remove
        effect_inertia[ad] = None

        try:
            task = self._execute(runner)
        except BaseException as reason:  # noqa: BLE001
            runner.epoch = False
            cleanup = ad._dispose()
            if inspect.isawaitable(cleanup):
                _schedule_coro(self.ctx, cleanup)
            raise reason

        if inspect.iscoroutine(task):
            execute_task = _create_task(task)
            ad._execute_task = execute_task

            async def _guard() -> None:
                try:
                    await execute_task
                except BaseException as reason:  # noqa: BLE001
                    runner.epoch = False
                    cleanup = ad._dispose()
                    if inspect.isawaitable(cleanup):
                        await cleanup

            _create_task(_guard())
        else:
            ad._execute_task = None

        return ad

    def get_effects(self) -> list[EffectMeta]:
        return [
            dispose._effect_meta  # type: ignore[attr-defined]
            for dispose in self._disposables
            if getattr(dispose, "_effect_meta", None) is not None
        ]

    # ------------------------------------------------------------------
    # lifecycle state machine
    # ------------------------------------------------------------------

    def _get_state(self) -> FiberState:
        if self.uid is None:
            return FiberState.DISPOSED
        if self._error is not None:
            return FiberState.FAILED
        if self._runner.epoch != INACTIVE:
            return FiberState.ACTIVE
        return FiberState.PENDING

    def _update_state(self, callback: Callable[[], FiberState | None]) -> None:
        old = self.state
        result = callback()
        self.state = result if result is not None else self._get_state()
        if old == self.state:
            return
        self.ctx.emit("internal/status", self, old)
        # only notify on transitions between ACTIVE and non-ACTIVE
        if old != FiberState.ACTIVE and self.state != FiberState.ACTIVE:
            return
        reflect = self.ctx.reflect
        for impl in list(reflect.store.values()):
            if impl.fiber is not self:
                continue
            reflect.notify([impl.name])

    def _check_impl(self, name: str) -> None:
        impl = self.ctx.reflect._get_impl(name, True)
        if not impl:
            self._store.pop(name, None)
            return
        check = impl.check
        if check is not None:
            try:
                if not check():
                    self._store.pop(name, None)
                    return
            except Exception as error:  # noqa: BLE001
                impl.fiber.ctx.logger.error(error)
                self._store.pop(name, None)
                return
        self._store[name] = impl

    def _refresh(self) -> None:
        epoch: Any = ""
        for name in self.inject:
            impl = self._store.get(name)
            if not impl:
                epoch = INACTIVE
                break
            epoch += ":" + str(impl.fiber.uid)
        self._set_epoch(epoch)

    def _set_epoch(self, epoch: Any) -> None:
        old = self._runner.epoch
        if epoch == old:
            return
        self._runner.epoch = epoch
        if self.inertia:
            return

        def cb() -> FiberState:
            if epoch != INACTIVE and old == INACTIVE:
                self.inertia = _create_task(self._reload())
                return FiberState.LOADING
            else:
                self.inertia = _create_task(self._unload())
                return FiberState.UNLOADING

        self._update_state(cb)

    def _resolve_config(self, config: Any) -> Any:
        config = self.ctx.waterfall(self, "internal/config", config, lambda *a: config)
        return resolve_config(self.runtime, config) if self.runtime else config

    async def _reload(self) -> None:
        self.store = dict(self._store)
        old_epoch = self._runner.epoch
        try:
            await asyncio.sleep(0)  # Promise.resolve() checkpoint
            if self._runner.epoch == old_epoch:
                self.config = self._resolve_config(self._config)
                task = self._execute(self._runner)
                if inspect.iscoroutine(task):
                    await task
                self._error = None
        except BaseException as reason:  # noqa: BLE001
            self.ctx.logger.error(reason)
            self._error = reason
            self._runner.epoch = INACTIVE

        def cb() -> FiberState | None:
            if self._runner.epoch == old_epoch:
                self.inertia = None
                return None
            else:
                self.inertia = _create_task(self._unload())
                return FiberState.UNLOADING

        self._update_state(cb)

    async def _unload(self) -> None:
        disposers = self._disposables.clear()

        async def _one(d: Disposable) -> None:
            try:
                await asyncio.sleep(0)  # checkpoint
                res = run_disposable(d)
                if inspect.isawaitable(res):
                    await res
            except BaseException as error:  # noqa: BLE001
                self.ctx.logger.error(error)

        if disposers:
            await asyncio.gather(*(_one(d) for d in disposers))
        self.store = None

        def cb() -> FiberState | None:
            if self._runner.epoch == INACTIVE:
                self.inertia = None
                return None
            else:
                self.inertia = _create_task(self._reload())
                return FiberState.LOADING

        self._update_state(cb)

    # ------------------------------------------------------------------
    # public lifecycle API
    # ------------------------------------------------------------------

    async def await_(self) -> "Fiber":
        """Wait for current lifecycle work and rethrow startup errors."""
        while self.inertia:
            await self.inertia
        if self._error is not None:
            raise self._error
        return self

    def __await__(self):
        # A Fiber is awaitable: ``await fiber`` settles once loading finished.
        return self.await_().__await__()

    async def restart(self) -> None:
        """Dispose and immediately reload this plugin with its current config."""
        self.assert_active()
        self._set_epoch(INACTIVE)
        self._refresh()
        await self.await_()

    def update(self, config: Any, no_save: bool = False) -> Any:
        """Validate and apply new config, then restart the plugin."""
        self.assert_active()
        self._config = config
        if self.state != FiberState.ACTIVE:
            self._error = None
            self._set_epoch(INACTIVE)
            self._refresh()
            return None
        config = self._resolve_config(config)

        def default_next(*a: Any) -> Any:
            self.config = config
            self._error = None
            return self.restart()

        return self.ctx.waterfall(self, "internal/update", config, no_save, default_next)


# ---------------------------------------------------------------------------
# _ProtoDict — a prototypally-inheriting dict (Object.create(parent) in TS)
# ---------------------------------------------------------------------------


class _ProtoDict:
    """A dict whose reads fall through to a parent dict (prototype chain)."""

    __slots__ = ("_own", "_parent")

    def __init__(self, parent: "_ProtoDict | None" = None) -> None:
        self._own: dict = {}
        self._parent = parent

    def __getitem__(self, key: Any) -> Any:
        if key in self._own:
            return self._own[key]
        if self._parent is not None:
            return self._parent[key]
        raise KeyError(key)

    def __setitem__(self, key: Any, value: Any) -> None:
        self._own[key] = value

    def __contains__(self, key: Any) -> bool:
        if key in self._own:
            return True
        return self._parent is not None and key in self._parent

    def get(self, key: Any, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def has_own(self, key: Any) -> bool:
        return key in self._own

    def parent_chain(self):
        cur = self
        while cur is not None:
            yield cur
            cur = cur._parent


__all__ = [
    "Fiber",
    "FiberState",
    "CordisError",
    "ValidationError",
    "AsyncDisposable",
    "EffectMeta",
    "resolve_config",
    "run_disposable",
    "effect_inertia",
    "emit_plugin_disposed",
    "_ProtoDict",
]
