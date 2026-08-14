"""Component lifecycle — the FiberState machine (calculus transitions)."""
import asyncio

from cordis import Context, FiberState


def run(coro):
    return asyncio.run(coro)


async def _states_pending_loading_active():
    root = Context()
    seen = []

    def consumer(ctx, config):
        seen.append("active")
        return lambda: seen.append("off")

    consumer.inject = ["dep"]

    fiber = await root.plugin(consumer)
    assert fiber.state == FiberState.PENDING

    class Dep:
        provide = "dep"

    def dep_plugin(ctx, config):
        ctx.reflect.provide("dep", Dep())

    await root.plugin(dep_plugin)
    await fiber.await_()
    assert fiber.state == FiberState.ACTIVE
    assert seen == ["active"]


async def _failure_records_error():
    root = Context()

    def boom(ctx, config):
        raise RuntimeError("boom")

    fiber = root.plugin(boom)  # do not await — loading will fail
    raised = False
    try:
        await fiber.await_()
    except RuntimeError:
        raised = True
    assert raised
    assert fiber.state == FiberState.FAILED


async def _restart_reloads():
    root = Context()
    loads = {"n": 0}

    def plugin(ctx, config):
        loads["n"] += 1
        return lambda: loads.setdefault("off", 0)

    fiber = await root.plugin(plugin)
    assert loads["n"] == 1
    await fiber.restart()
    assert loads["n"] == 2


async def _update_reconfigures():
    root = Context()
    seen = []

    def plugin(ctx, config):
        seen.append(config.get("v"))
        return lambda: seen.append("off")

    fiber = await root.plugin(plugin, {"v": 1})
    assert seen == [1]
    task = fiber.update({"v": 2})
    if task is not None:
        await task
    await fiber.await_()
    assert seen == [1, "off", 2]


async def _awaitable_fiber():
    root = Context()
    fiber = await root.plugin(lambda ctx, cfg: None)
    # `await fiber` resolves to the settled fiber
    again = await fiber
    assert again is fiber


def test_states_pending_loading_active():
    run(_states_pending_loading_active())


def test_failure_records_error():
    run(_failure_records_error())


def test_restart_reloads():
    run(_restart_reloads())


def test_update_reconfigures():
    run(_update_reconfigures())


def test_awaitable_fiber():
    run(_awaitable_fiber())
