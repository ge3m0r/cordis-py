"""Disposal-aware timer helpers (port of ``@cordisjs/plugin-timer``).

Provides ``ctx.timer`` with ``setTimeout`` / ``setInterval`` / ``debounce`` /
``throttle``, each scheduled through ``ctx.fiber.effect`` so pending timers
are cancelled when the owning fiber unloads.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, Callable

from ..service import Service
from ..events import _schedule_coro


class TimerService(Service):
    """Disposal-aware timeout, interval, throttle, and debounce helpers."""

    provide = "timer"

    def __init__(self, ctx, config: Any = None) -> None:
        super().__init__(ctx)
        ctx.reflect.mixin("timer", ["setTimeout", "setInterval", "debounce", "throttle"])

    # ------------------------------------------------------------------
    # timeout / interval
    # ------------------------------------------------------------------

    async def _run_timeout(self, callback: Callable, delay: float) -> None:
        await asyncio.sleep(delay / 1000)
        self._fire(callback)

    async def _run_interval(self, callback: Callable, delay: float) -> None:
        while True:
            await asyncio.sleep(delay / 1000)
            self._fire(callback)

    def _fire(self, callback: Callable) -> None:
        try:
            result = callback()
            if inspect.iscoroutine(result):
                _schedule_coro(self.ctx, result)
        except Exception as error:  # noqa: BLE001
            self.ctx.logger.error(error)

    def setTimeout(self, callback: Callable, delay: float) -> Any:
        def body():
            task = asyncio.ensure_future(self._run_timeout(callback, delay))
            return lambda: task.cancel()

        return self.ctx.fiber.effect(body, "ctx.setTimeout()")

    def setInterval(self, callback: Callable, delay: float) -> Any:
        def body():
            task = asyncio.ensure_future(self._run_interval(callback, delay))
            return lambda: task.cancel()

        return self.ctx.fiber.effect(body, "ctx.setInterval()")

    # ------------------------------------------------------------------
    # debounce / throttle
    # ------------------------------------------------------------------

    async def _run_debounced(self, callback: Callable, args: tuple, delay: float) -> None:
        await asyncio.sleep(delay / 1000)
        self._fire(lambda: callback(*args))

    def debounce(self, callback: Callable, delay: float) -> Callable:
        state: dict = {"task": None}

        def debounced(*args: Any) -> None:
            if state["task"] is not None:
                state["task"].cancel()
            state["task"] = asyncio.ensure_future(self._run_debounced(callback, args, delay))

        def cancel() -> None:
            if state["task"] is not None:
                state["task"].cancel()

        debounced.dispose = cancel  # type: ignore[attr-defined]
        self.ctx.fiber.effect(lambda: cancel, "ctx.debounce()")
        return debounced

    def throttle(self, callback: Callable, delay: float) -> Callable:
        state: dict = {"task": None, "last": 0.0, "pending": None}

        def debounced(*args: Any) -> None:
            import time

            now = time.monotonic()
            if state["task"] is None:
                # leading edge
                state["last"] = now
                self._fire(lambda: callback(*args))
                state["task"] = asyncio.ensure_future(self._trailing_wait(delay))
                state["pending"] = None
            else:
                # within the window: remember the last call for the trailing edge
                state["pending"] = args

        async def _trailing_wait(delay_ms: float) -> None:
            await asyncio.sleep(delay_ms / 1000)
            pending = state["pending"]
            state["task"] = None
            state["pending"] = None
            if pending is not None:
                debounced(*pending)

        def cancel() -> None:
            if state["task"] is not None:
                state["task"].cancel()

        debounced.dispose = cancel  # type: ignore[attr-defined]
        self.ctx.fiber.effect(lambda: cancel, "ctx.throttle()")
        return debounced


__all__ = ["TimerService"]
