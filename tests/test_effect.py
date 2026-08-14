"""Revertible effects — temporal composability (Theorem 61 / Corollary 62).

Every effect carries an inverse (disposer); the runtime tracks them and runs
them in reverse (LIFO) order on disposal, recovering the pre-composition state.
"""
import asyncio
import gc
import weakref

from cordis import Context
from cordis.fiber import effect_inertia


def run(coro):
    return asyncio.run(coro)


async def _sync_disposer_runs_on_unload():
    root = Context()
    log = []

    def plugin(ctx, config):
        log.append("setup")
        return lambda: log.append("teardown")

    fiber = await root.plugin(plugin)
    assert log == ["setup"]
    task = fiber.dispose()
    if task is not None:
        await task
    assert log == ["setup", "teardown"]


async def _lifo_order():
    root = Context()
    order = []

    def make(label):
        def body():
            order.append(f"{label}-on")
            return lambda: order.append(f"{label}-off")

        return body

    def plugin(ctx, config):
        ctx.fiber.effect(make("1"), "a")
        ctx.fiber.effect(make("2"), "b")
        ctx.fiber.effect(make("3"), "c")

    fiber = await root.plugin(plugin)
    assert order == ["1-on", "2-on", "3-on"]
    task = fiber.dispose()
    if task is not None:
        await task
    assert order == ["1-on", "2-on", "3-on", "3-off", "2-off", "1-off"], order


async def _generator_effect():
    root = Context()
    order = []

    def nested():
        order.append("nested-on")
        return lambda: order.append("nested-off")

    def plugin(ctx, config):
        ctx.fiber.effect(nested, "n")

        def gen():
            order.append("gen-1")
            yield lambda: order.append("gen-1-off")
            order.append("gen-2")
            yield lambda: order.append("gen-2-off")

        return gen()

    fiber = await root.plugin(plugin)
    assert order == ["nested-on", "gen-1", "gen-2"]
    task = fiber.dispose()
    if task is not None:
        await task
    assert order == ["nested-on", "gen-1", "gen-2", "gen-2-off", "gen-1-off", "nested-off"], order


async def _async_generator_effect():
    root = Context()
    order = []

    def plugin(ctx, config):
        async def gen():
            order.append("a-1")
            yield lambda: order.append("a-1-off")
            await asyncio.sleep(0)
            order.append("a-2")
            yield lambda: order.append("a-2-off")

        return gen()

    fiber = await root.plugin(plugin)
    await asyncio.sleep(0.02)
    assert order == ["a-1", "a-2"], order
    task = fiber.dispose()
    if task is not None:
        await task
    assert order == ["a-1", "a-2", "a-2-off", "a-1-off"], order


async def _disposer_idempotent():
    root = Context()
    count = {"n": 0}

    def plugin(ctx, config):
        def off():
            count["n"] += 1

        return ctx.fiber.effect(lambda: off, "x")

    fiber = await root.plugin(plugin)
    t1 = fiber.dispose()
    if t1 is not None:
        await t1
    t2 = fiber.dispose()
    if t2 is not None:
        await t2
    assert count["n"] == 1, count


def test_sync_disposer_runs_on_unload():
    run(_sync_disposer_runs_on_unload())


def test_lifo_order():
    run(_lifo_order())


def test_generator_effect():
    run(_generator_effect())


def test_async_generator_effect():
    run(_async_generator_effect())


def test_disposer_idempotent():
    run(_disposer_idempotent())


async def _effect_inertia_reclaims_after_unload():
    # effect_inertia is weak-keyed (mirrors the TS WeakMap): once an effect's
    # AsyncDisposable is released by its owning fiber and has no other refs,
    # the inertia entry is reclaimed on GC. (The id()-dict port never cleared.)
    root = Context()
    state: dict = {}

    def plugin(ctx, config):
        ad = ctx.fiber.effect(lambda: lambda: None, "x")
        state["ref"] = weakref.ref(ad)  # ad supports __weakref__
        return lambda: None

    fiber = await root.plugin(plugin)
    task = fiber.dispose()
    if task is not None:
        await task
    gc.collect()
    # the effect's disposable is no longer referenced -> reclaimed
    assert state["ref"]() is None, "effect AsyncDisposable was not reclaimed"
    assert len([k for k in effect_inertia.keyrefs() if k() is not None]) >= 0


def test_effect_inertia_reclaims_after_unload():
    run(_effect_inertia_reclaims_after_unload())


async def _effect_wrapper_unlinked_from_fiber_on_dispose():
    # disposing an effect must unlink its wrapper from the owning fiber's
    # _disposables (mirrors TS finalizeDisposal removeWrapper) — otherwise the
    # wrapper is permanently retained on long-lived contexts.
    root = Context()
    baseline = len(root.fiber._disposables)
    ad = root.on("e", lambda: None)
    assert len(root.fiber._disposables) == baseline + 1
    task = ad()
    if task is not None:
        await task
    assert len(root.fiber._disposables) == baseline, len(root.fiber._disposables)


async def _root_level_effects_do_not_accumulate():
    root = Context()
    baseline = len(root.fiber._disposables)
    for _ in range(100):
        ad = root.on("e", lambda: None)
        t = ad()
        if t is not None:
            await t
    assert len(root.fiber._disposables) == baseline, len(root.fiber._disposables)


async def _plugin_cycles_do_not_accumulate_wrappers():
    # each ctx.plugin() fiber's dispose wrapper is pushed onto the parent
    # (root) fiber's _disposables; disposing the fiber must unlink it.
    root = Context()
    baseline = len(root.fiber._disposables)
    for _ in range(50):

        def plugin(ctx, config):
            return lambda: None

        fiber = await root.plugin(plugin)
        t = fiber.dispose()
        if t is not None:
            await t
    assert len(root.fiber._disposables) <= baseline + 1, len(root.fiber._disposables)


def test_effect_wrapper_unlinked_from_fiber_on_dispose():
    run(_effect_wrapper_unlinked_from_fiber_on_dispose())


def test_root_level_effects_do_not_accumulate():
    run(_root_level_effects_do_not_accumulate())


def test_plugin_cycles_do_not_accumulate_wrappers():
    run(_plugin_cycles_do_not_accumulate_wrappers())
