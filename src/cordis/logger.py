"""Built-in logging service.

Faithful port of ``vendor/cordis/src/logger.ts``. ``LoggerService`` is callable
(``ctx.logger(name)`` returns a named :class:`Logger`) and also exposes severity
methods directly (``ctx.logger.info(...)``). Named loggers carry an inherited
name resolved from the intercept chain and the owning fiber.
"""

from __future__ import annotations

import json
import time
from enum import IntEnum
from typing import Any, Callable

from .symbols import symbols
from .utils import Tracker, create_callable, get_traceable

if False:  # TYPE_CHECKING
    from .context import Context
    from .fiber import Fiber


class LoggerLevel(IntEnum):
    ERROR = 0
    INFO = 1
    WARN = 2
    DEBUG = 3


LOGGER_TYPES = ("error", "info", "warn", "debug")

C16 = [6, 2, 3, 4, 5, 1]
C256 = [
    20, 21, 26, 27, 32, 33, 38, 39, 40, 41, 42, 43, 44, 45, 56, 57, 62, 63, 68,
    69, 74, 75, 76, 77, 78, 79, 80, 81, 92, 93, 98, 99, 112, 113, 129, 134, 135,
    148, 149, 160, 161, 162, 163, 164, 165, 166, 167, 168, 169, 170, 171, 172,
    173, 178, 179, 184, 185, 196, 197, 198, 199, 200, 201, 202, 203, 204, 205,
    206, 207, 208, 209, 214, 215, 220, 221,
]


def default_format(exporter: Any, message: "Message") -> str:
    args = list(message.args)
    if args and isinstance(args[0], BaseException):
        args[0] = "".join(_format_exception(args[0]))
        args.insert(0, "%s")
    elif not args or not isinstance(args[0], str):
        args.insert(0, "%o")

    fmt: str = args.pop(0)
    out_parts: list[str] = []
    i = 0
    while i < len(fmt):
        if fmt[i] == "%" and i + 1 < len(fmt):
            ch = fmt[i + 1]
            if ch == "%":
                out_parts.append("%")
                i += 2
                continue
            formatters = getattr(exporter, "formatters", None) or {}
            fn = formatters.get(ch) or DEFAULT_FORMATTERS.get(ch)
            if callable(fn):
                value = args.pop(0) if args else None
                out_parts.append(str(fn(value, exporter, message)))
                i += 2
                continue
        out_parts.append(fmt[i])
        i += 1
    fmt = "".join(out_parts)

    o_fn = (getattr(exporter, "formatters", None) or {}).get("o") or DEFAULT_FORMATTERS["o"]
    for arg in args:
        if isinstance(arg, dict) or hasattr(arg, "__dict__"):
            fmt += " " + str(o_fn(arg, exporter, message))
        else:
            fmt += " " + str(arg)

    max_length = getattr(exporter, "maxLength", 10240)
    lines = []
    for line in fmt.splitlines() or [""]:
        if len(line) > max_length:
            lines.append(line[:max_length] + "...")
        else:
            lines.append(line)
    return "\n".join(lines)


def _format_exception(error: BaseException) -> list[str]:
    import traceback as tb

    return [f"Error: {error}\n"] + tb.format_exception(type(error), error, error.__traceback__)


DEFAULT_FORMATTERS: dict[str, Callable] = {
    "s": lambda value, *_: str(value),
    "d": lambda value, *_: int(value),
    "i": lambda value, *_: int(value),
    "f": lambda value, *_: float(value),
    "o": lambda value, *_: json.dumps(value, default=str, indent=0),
    "O": lambda value, *_: json.dumps(value, default=str, indent=0),
    "c": lambda *_: "",
}


class Message:
    __slots__ = ("sn", "ts", "name", "type", "level", "args", "fiber")

    def __init__(self, sn: int, ts: float, name: str, type: str, level: int, args: list, fiber: Any = None) -> None:
        self.sn = sn
        self.ts = ts
        self.name = name
        self.type = type
        self.level = level
        self.args = args
        self.fiber = fiber


class Logger:
    """Logger facade for one named subsystem."""

    def __init__(self, name: str, service: "LoggerService", level: int | None = None, meta: dict | None = None) -> None:
        self.name = name
        self.service = service
        self.level = level
        self.meta = meta or {}
        self.error = self._method("error", LoggerLevel.ERROR)
        self.info = self._method("info", LoggerLevel.INFO)
        self.warn = self._method("warn", LoggerLevel.WARN)
        self.debug = self._method("debug", LoggerLevel.DEBUG)

    def _method(self, type: str, level: int) -> Callable:
        service = self.service

        def log(*args: Any) -> None:
            if len(args) == 1 and isinstance(args[0], BaseException):
                cause = getattr(args[0], "__cause__", None)
                if cause is not None:
                    log(cause)
                else:
                    errors = getattr(args[0], "errors", None)
                    if isinstance(errors, list):
                        for err in errors:
                            log(err)
                        return

            sn = service._sn_message = service._sn_message + 1
            ts = time.time()
            for exporter in list(service.exporters.values()):
                levels = exporter.get("levels") if isinstance(exporter, dict) else getattr(exporter, "levels", None)
                target_level = levels.get(self.name) if levels else None
                if target_level is None and levels:
                    target_level = levels.get("default")
                if target_level is None:
                    target_level = self.level if self.level is not None else LoggerLevel.INFO
                if target_level < level:
                    continue
                message = Message(sn, ts, self.name, type, level, list(args), self.meta.get("fiber"))
                export_fn = exporter["export"] if isinstance(exporter, dict) else exporter.export
                export_fn(message)

        return log


def _hyphenate(name: str) -> str:
    import re

    return re.sub(r"(?<!^)(?=[A-Z])", "-", name).lower()


class LoggerService:
    """Callable ``ctx.logger`` service."""

    _tracker: Tracker

    def __init__(self, ctx: "Context") -> None:
        self.ctx = ctx
        self._tracker = Tracker(property="ctx", noShadow=True)
        self.buffer_size = 1000
        self.buffer: list[Message] = []
        self._sn_message = 0
        self._sn_exporter = 0
        self.exporters: dict[int, Any] = {}
        create_callable("logger", self, self._tracker)

        # built-in buffer exporter
        def _export(message: Message) -> None:
            self.buffer.append(message)
            if len(self.buffer) > self.buffer_size:
                self.buffer = self.buffer[-self.buffer_size :]

        self.exporter({"colors": 3, "export": _export})

    def __call__(self, name: str | None = None) -> Logger:
        return self._invoke(name)

    def exporter(self, exporter: Any) -> Callable:
        def body() -> Callable:
            sn = self._sn_exporter = self._sn_exporter + 1
            self.exporters[sn] = exporter
            return lambda: self.exporters.pop(sn, None) and True

        return self.ctx.fiber.effect(body, "ctx.logger.exporter()")

    def _resolve_config(self) -> dict:
        intercept = self.ctx._intercept
        configs: list[dict] = []
        while intercept is not None and "logger" in intercept:
            if intercept.has_own("logger"):
                configs.insert(0, intercept["logger"])
            intercept = intercept._parent
        merged: dict = {}
        for c in configs:
            if isinstance(c, dict):
                merged.update(c)
        return merged

    def _invoke(self, name: str | None = None) -> Logger:
        config = self._resolve_config()
        # caller fiber (account for shadow)
        caller_ctx = self.ctx
        fiber = caller_ctx.fiber
        if name is None:
            name = config.get("name")
        if name is None:
            name = _hyphenate(fiber.name)
        return Logger(name=name, service=self, level=config.get("level"))


# Direct severity methods on the service (TS static block).
for _type, _level in [("error", LoggerLevel.ERROR), ("info", LoggerLevel.INFO), ("warn", LoggerLevel.WARN), ("debug", LoggerLevel.DEBUG)]:
    def _make(t: str = _type, l: int = _level):
        def method(self: "LoggerService", *args: Any) -> None:
            logger = self()
            getattr(logger, t)(*args)

        return method

    setattr(LoggerService, _type, _make())


__all__ = ["LoggerService", "Logger", "LoggerLevel", "Message", "default_format"]
