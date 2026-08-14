"""Quick smoke check for the cordis core (run directly during development)."""
import asyncio
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cordis import Context, Service  # noqa: E402


class Counter(Service):
    provide = "counter"

    def __init__(self, ctx, name="counter"):
        super().__init__(ctx, name)
        self.value = 0

    def next(self):
        self.value += 1
        return self.value


def greeter(ctx, config):
    dispose = ctx.on(
        "app/ready",
        lambda message: ctx.logger.info("%s #%d", message, ctx.counter.next()),
    )
    return dispose


greeter.inject = ["counter"]


async def main():
    root = Context()
    await root.plugin(Counter)
    await root.plugin(greeter)

    root.emit("app/ready", "started")
    root.emit("app/ready", "again")

    # effect + disposal: revertible effect
    log = []

    def effect_plugin(ctx, config):
        log.append("setup")
        return lambda: log.append("teardown")

    fiber = await root.plugin(effect_plugin)
    assert "setup" in log
    # dispose the fiber and await its cleanup
    task = fiber.dispose()
    if task is not None:
        await task
    await asyncio.sleep(0.01)
    assert "teardown" in log, log

    print("SMOKE OK", log)


asyncio.run(main())
