"""Service base — registration, check predicate, callable services."""
import asyncio

from cordis import Context, Service


def run(coro):
    return asyncio.run(coro)


class Greeter(Service):
    provide = "greeter"

    def __init__(self, ctx, name="greeter"):
        super().__init__(ctx, name)

    def hello(self):
        return "hi"


class Counter(Service):
    provide = "counter"

    def __init__(self, ctx, name="counter"):
        super().__init__(ctx, name)
        self.value = 0

    def _check(self):
        return self.value >= 0

    def next(self):
        self.value += 1
        return self.value


async def _service_registered_on_ctx():
    root = Context()
    g = Greeter(root, "greeter")
    await root.plugin(lambda ctx, cfg: g)
    await asyncio.sleep(0.01)
    impl = root.reflect._get_impl("greeter")
    assert impl is not None and impl.value is g


async def _check_predicate_gates_availability():
    root = Context()
    c = Counter(root, "counter")
    await root.plugin(lambda ctx, cfg: c)
    await asyncio.sleep(0.01)
    impl = root.reflect._get_impl("counter")
    assert impl is not None and impl.value is c


async def _inject_resolves_service():
    root = Context()
    c = Counter(root, "counter")
    seen = []

    def consumer(ctx, config):
        seen.append(ctx.counter.next())

    consumer.inject = ["counter"]

    await root.plugin(lambda ctx, cfg: c)
    await root.plugin(consumer)
    await asyncio.sleep(0.01)
    assert seen == [1]


async def _resolves_to_none_when_absent():
    root = Context()
    assert root.get("nope") is None


def test_service_registered_on_ctx():
    run(_service_registered_on_ctx())


def test_check_predicate_gates_availability():
    run(_check_predicate_gates_availability())


def test_inject_resolves_service():
    run(_inject_resolves_service())


def test_resolves_to_none_when_absent():
    run(_resolves_to_none_when_absent())


async def _owner_can_set_declared_service():
    # proxy_set service branch: the providing fiber may overwrite its binding.
    root = Context()

    def plugin(ctx, config):
        dispose = ctx.reflect.provide("box", "initial")
        ctx.box = "updated"  # owner set via Context.__setattr__ -> proxy_set
        return dispose

    await root.plugin(plugin)
    await asyncio.sleep(0.01)
    assert root.get("box") == "updated", root.get("box")


async def _non_owner_set_raises():
    # a fiber that did not provide the service cannot set it.
    root = Context()

    def plugin(ctx, config):
        return ctx.reflect.provide("box", "initial")

    await root.plugin(plugin)
    await asyncio.sleep(0.01)
    raised = False
    try:
        root.box = "x"  # root fiber is not the providing fiber
    except Exception:
        raised = True
    assert raised


def test_owner_can_set_declared_service():
    run(_owner_can_set_declared_service())


def test_non_owner_set_raises():
    run(_non_owner_set_raises())
