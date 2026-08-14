"""Timer — disposal-aware timeout/interval/debounce."""
import asyncio

from cordis import Context
from cordis.timer import TimerService


def run(coro):
    return asyncio.run(coro)


async def _set_timeout_fires():
    root = Context()
    await root.plugin(TimerService, {})
    fired = []
    root.timer.setTimeout(lambda: fired.append(1), 5)
    await asyncio.sleep(0.05)
    assert fired == [1]


async def _set_timeout_cancelled_on_unload():
    root = Context()
    timer_fiber = await root.plugin(TimerService, {})
    fired = []
    timer_fiber.ctx.timer.setTimeout(lambda: fired.append(1), 50)
    # unload the timer's fiber -> pending timeout cancelled
    t = timer_fiber.dispose()
    if t is not None:
        await t
    await asyncio.sleep(0.06)
    assert fired == []


async def _set_interval_repeats():
    root = Context()
    await root.plugin(TimerService, {})
    n = {"i": 0}
    root.timer.setInterval(lambda: n.__setitem__("i", n["i"] + 1), 5)
    await asyncio.sleep(0.04)
    assert n["i"] >= 2


async def _debounce_coalesces():
    root = Context()
    await root.plugin(TimerService, {})
    calls = {"n": 0}
    d = root.timer.debounce(lambda: calls.__setitem__("n", calls["n"] + 1), 20)
    d()
    d()
    d()
    await asyncio.sleep(0.04)
    assert calls["n"] == 1


def test_set_timeout_fires():
    run(_set_timeout_fires())


def test_set_timeout_cancelled_on_unload():
    run(_set_timeout_cancelled_on_unload())


def test_set_interval_repeats():
    run(_set_interval_repeats())


def test_debounce_coalesces():
    run(_debounce_coalesces())
