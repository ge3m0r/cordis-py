"""Cordis — a meta-framework of spatiotemporal composability (Python port).

This package is a faithful, runtime-identical port of the vendored Cordis v4
framework (see ``vendor/cordis/`` and the paper "A Programming Paradigm for
Spatiotemporal Composability"). It provides:

* :class:`Context` — the unified context (effect + coeffect) and dependency
  container.
* :class:`Fiber` / :class:`FiberState` — component lifecycle and revertible
  effects (``ctx.effect``).
* :class:`ReflectService` — coeffect resolution (``provide``/``notify``).
* :class:`RegistryService` — plugin loading (``ctx.plugin`` / ``ctx.inject``).
* :class:`EventsService` — typed events (``emit``/``parallel``/``serial``/
  ``bail``/``waterfall``).
* :class:`Service` — base class for named services on ``ctx``.
"""

from __future__ import annotations

from .symbols import Symbol, INACTIVE, symbols
from .utils import (
    DisposableList,
    Tracker,
    get_traceable,
    with_props,
    create_callable,
    compose_error,
    build_outer_stack,
)
from .events import EventsService, Hook, EventOptions, AggregateError, is_bailed
from .fiber import (
    Fiber,
    FiberState,
    CordisError,
    ValidationError,
    AsyncDisposable,
    EffectMeta,
    resolve_config,
    run_disposable,
)
from .reflect import ReflectService, Impl, Property
from .registry import RegistryService, Plugin, Inject
from .service import Service
from .logger import LoggerService, Logger, LoggerLevel, Message
from .context import Context
from .loader import LoaderService, Entry, resolve_plugin, include, HmrService, hmr, group
from .timer import TimerService
from .logger_console import console_logger

__all__ = [
    "Context",
    "Fiber",
    "FiberState",
    "CordisError",
    "ValidationError",
    "AsyncDisposable",
    "EffectMeta",
    "ReflectService",
    "Impl",
    "Property",
    "RegistryService",
    "Plugin",
    "Inject",
    "Service",
    "EventsService",
    "Hook",
    "EventOptions",
    "AggregateError",
    "is_bailed",
    "LoggerService",
    "Logger",
    "LoggerLevel",
    "Message",
    "Symbol",
    "INACTIVE",
    "symbols",
    "DisposableList",
    "Tracker",
    "get_traceable",
    "with_props",
    "create_callable",
    "compose_error",
    "build_outer_stack",
    "resolve_config",
    "run_disposable",
    "LoaderService",
    "Entry",
    "resolve_plugin",
    "include",
    "HmrService",
    "hmr",
    "group",
    "TimerService",
    "console_logger",
]
