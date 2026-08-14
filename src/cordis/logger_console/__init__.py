"""Console exporter for the built-in logger (port of ``@cordisjs/plugin-logger-console``).

A function plugin that registers an exporter writing formatted log lines to
``stdout``/``stderr``. Registered through ``ctx.logger.exporter`` so it unloads
with the owning fiber.
"""

from __future__ import annotations

import sys
from typing import Any

from ..logger import LoggerLevel, default_format


def console_logger(ctx: Any, config: Any = None) -> Any:
    """Register a console exporter on ``ctx.logger``; returns its disposer."""
    colors = config.get("colors", 2) if isinstance(config, dict) else 2
    max_length = config.get("maxLength", 10240) if isinstance(config, dict) else 10240

    exporter: dict = {
        "colors": colors,
        "maxLength": max_length,
        "levels": {"default": LoggerLevel.DEBUG},
        "formatters": {},
    }

    def _export(message: Any) -> None:
        try:
            line = default_format(exporter, message)
        except Exception:  # noqa: BLE001
            line = " ".join(str(a) for a in message.args)
        stream = sys.stderr if message.type == "error" else sys.stdout
        print(f"[{message.name}] {line}", file=stream)

    # assign after the def so the name is bound (avoids UnboundLocalError)
    exporter["export"] = _export
    return ctx.logger.exporter(exporter)


console_logger.name = "@cordisjs/plugin-logger-console"  # type: ignore[attr-defined]


__all__ = ["console_logger"]
