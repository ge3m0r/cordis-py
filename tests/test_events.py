"""Typed events — dispatch modes (emit / parallel / serial / bail / waterfall)."""
import asyncio

from cordis import Context, is_bailed


def run(coro):
    return asyncio.run(coro)


async def _emit_runs_all_sync():
    root = Context()
    log = []
    root.on("e", lambda: log.append(1))
    root.on("e", lambda: log.append(2))
    root.emit("e")
    assert log == [1, 2]


async def _parallel_awaits_all():
    root = Context()
    log = []

    async def slow(n):
        await asyncio.sleep(0.005)
        log.append(n)

    root.on("p", lambda: slow(1))
    root.on("p", lambda: slow(2))
    await root.parallel("p")
    assert sorted(log) == [1, 2]


async def _parallel_aggregates_errors():
    root = Context()

    async def boom():
        raise ValueError("x")

    root.on("pe", lambda: boom())
    raised = False
    try:
        await root.parallel("pe")
    except Exception:
        raised = True
    assert raised


async def _serial_bails():
    root = Context()
    calls = []

    def first():
        calls.append("first")
        return None  # not bailed

    def second():
        calls.append("second")
        return "stop"  # bailed

    def third():
        calls.append("third")

    root.on("s", first)
    root.on("s", second)
    root.on("s", third)
    result = await root.serial("s")
    assert result == "stop"
    assert calls == ["first", "second"]


async def _bail_sync():
    root = Context()
    calls = []

    root.on("b", lambda: None)
    root.on("b", lambda: "hit")
    root.on("b", lambda: calls.append("never"))
    result = root.bail("b")
    assert result == "hit"


def test_is_bailed():
    assert is_bailed("x")
    assert is_bailed(1)
    assert not is_bailed(None)
    assert not is_bailed(False)
    assert is_bailed(0)  # 0 is non-null and non-False -> bailed (matches TS isBailed)


async def _waterfall_chain():
    root = Context()
    trace = []

    def outer(arg, next_cb):
        trace.append(f"outer:{arg}")
        res = next_cb()
        trace.append(f"outer-end:{res}")
        return res

    def inner(arg, next_cb):
        trace.append(f"inner:{arg}")
        return next_cb()

    root.on("w", outer)
    root.on("w", inner)

    def base():
        trace.append("base")
        return 42

    result = root.waterfall("w", "hello", base)
    assert result == 42
    assert trace[0] == "outer:hello"
    assert trace == ["outer:hello", "inner:hello", "base", "outer-end:42"], trace


async def _waterfall_veto():
    root = Context()
    ran = [False]

    def vetoer(arg, next_cb):
        return "vetoed"  # does not call next

    def downstream(arg, next_cb):
        ran[0] = True

    root.on("v", vetoer)
    root.on("v", downstream)
    result = root.waterfall("v", "x", lambda: "default")
    assert result == "vetoed"
    assert not ran[0]


async def _once():
    root = Context()
    n = {"i": 0}
    root.once("o", lambda: n.__setitem__("i", n["i"] + 1))
    root.emit("o")
    root.emit("o")
    assert n["i"] == 1


async def _prepend():
    root = Context()
    order = []
    root.on("e", lambda: order.append("late"))
    root.on("e", lambda: order.append("early"), True)
    root.emit("e")
    assert order == ["early", "late"]


def test_emit_runs_all_sync():
    run(_emit_runs_all_sync())


def test_parallel_awaits_all():
    run(_parallel_awaits_all())


def test_parallel_aggregates_errors():
    run(_parallel_aggregates_errors())


def test_serial_bails():
    run(_serial_bails())


def test_bail_sync():
    run(_bail_sync())


def test_waterfall_chain():
    run(_waterfall_chain())


def test_waterfall_veto():
    run(_waterfall_veto())


def test_once():
    run(_once())


def test_prepend():
    run(_prepend())
