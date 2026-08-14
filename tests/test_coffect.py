"""Reactive coeffects — spatial composability (Definition 26 / Theorem 63).

A plugin declares its dependencies via ``inject`` (the specification); when a
required service is provided or unloaded, the runtime notifies the dependent
fiber, recomputes its epoch, and activates (reload) / deactivates (unload) it.
"""
import asyncio

from cordis import Context, Service


def run(coro):
    return asyncio.run(coro)


class Counter(Service):
    provide = "counter"

    def __init__(self, ctx, name="counter"):
        super().__init__(ctx, name)
        self.value = 0

    def next(self):
        self.value += 1
        return self.value


async def _inject_waits_then_activates():
    root = Context()
    seen = []

    def consumer(ctx, config):
        seen.append(ctx.counter.value)
        return lambda: seen.append("off")

    consumer.inject = ["counter"]

    # load consumer before the dependency exists -> stays PENDING
    fiber = await root.plugin(consumer)
    assert fiber.state.name == "PENDING", fiber.state
    assert seen == []

    # provide the dependency -> reactive activation
    await root.plugin(Counter)
    await fiber.await_()
    assert fiber.state.name == "ACTIVE"
    assert seen == [0]


async def _dependency_removal_deactivates():
    root = Context()
    seen = []

    def consumer(ctx, config):
        seen.append("on")
        return lambda: seen.append("off")

    consumer.inject = ["counter"]

    counter = await root.plugin(Counter)
    fiber = await root.plugin(consumer)
    await fiber.await_()
    assert seen == ["on"]

    # remove the dependency -> consumer deactivates (inverse runs)
    task = counter.dispose()
    if task is not None:
        await task
    await asyncio.sleep(0.01)
    assert "off" in seen, seen


async def _dependency_identity_change_reloads():
    root = Context()
    calls = {"n": 0}

    def consumer(ctx, config):
        calls["n"] += 1
        return lambda: calls.setdefault("off", 0) or calls.__setitem__("off", calls["off"] + 1)

    consumer.inject = ["counter"]

    first = await root.plugin(Counter)
    fiber = await root.plugin(consumer)
    await fiber.await_()
    assert calls["n"] == 1

    # replace the provider: dispose old, provide new -> epoch changes -> reload
    t = first.dispose()
    if t is not None:
        await t
    await root.plugin(Counter)
    await fiber.await_()
    assert calls["n"] == 2, calls


async def _isolation_scopes():
    root = Context()
    iso_a = root.isolate("counter", label=__name__ + ".a")
    iso_b = root.isolate("counter", label=__name__ + ".b")

    ca = Counter(iso_a, "counter")
    cb = Counter(iso_b, "counter")
    ca.value = 100
    cb.value = 200

    # both registered under the same name but different realms
    await root.plugin(lambda ctx, cfg: ca)
    await root.plugin(lambda ctx, cfg: cb)
    await asyncio.sleep(0.01)
    assert iso_a.get("counter").value == 100
    assert iso_b.get("counter").value == 200


async def _intercept_config_merge():
    root = Context()
    ctx = root.intercept("counter", {"start": 41})
    intercepted = Counter(ctx, "counter")
    await root.plugin(lambda c, cfg: intercepted)
    await asyncio.sleep(0.01)
    # resolveConfig merges the intercept chain
    merged = intercepted.resolve_config()
    assert merged.get("start") == 41


def test_inject_waits_then_activates():
    run(_inject_waits_then_activates())


def test_dependency_removal_deactivates():
    run(_dependency_removal_deactivates())


def test_dependency_identity_change_reloads():
    run(_dependency_identity_change_reloads())


def test_isolation_scopes():
    run(_isolation_scopes())


def test_intercept_config_merge():
    run(_intercept_config_merge())
