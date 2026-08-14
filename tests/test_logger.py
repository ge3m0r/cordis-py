"""Logger — levels, exporters, named facades."""
import asyncio
import contextlib
import io

from cordis import Context
from cordis.logger import LoggerService, LoggerLevel, Message
from cordis.logger_console import console_logger


def run(coro):
    return asyncio.run(coro)


async def _named_logger_logs_to_exporters():
    root = Context()
    captured = []

    def export(message: Message):
        captured.append((message.name, message.type, message.args))

    root.logger.exporter({"export": export, "levels": {"default": LoggerLevel.DEBUG}})
    logger = root.logger("my-svc")
    logger.info("hello %s", "world")
    assert captured == [("my-svc", "info", ["hello world", "world"])] or captured[0][0] == "my-svc"


async def _level_filtering():
    root = Context()
    captured = []

    def export(message: Message):
        captured.append(message.type)

    # only ERROR (0) and INFO (1) at default level WARN threshold set to 0
    root.logger.exporter({"export": export, "levels": {"default": LoggerLevel.ERROR}})
    logger = root.logger("x")
    logger.info("should-be-filtered")
    logger.error("kept")
    types_seen = [c for c in captured]
    assert "error" in types_seen
    assert "info" not in types_seen


async def _direct_severity_on_service():
    root = Context()
    captured = []
    root.logger.exporter({"export": lambda m: captured.append(m.type), "levels": {"default": LoggerLevel.DEBUG}})
    root.logger.info("direct")
    assert "info" in captured


def test_named_logger_logs_to_exporters():
    run(_named_logger_logs_to_exporters())


def test_level_filtering():
    run(_level_filtering())


def test_direct_severity_on_service():
    run(_direct_severity_on_service())


async def _console_logger_plugin_registers_and_prints():
    root = Context()
    await root.plugin(console_logger)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        root.logger("svc").info("hello %s", "world")
    out = buf.getvalue()
    assert "svc" in out and "hello" in out, out


def test_console_logger_plugin_registers_and_prints():
    run(_console_logger_plugin_registers_and_prints())


async def _console_logger_unloads_with_fiber():
    root = Context()
    fiber = await root.plugin(console_logger)
    before = len(root.logger.exporters)
    task = fiber.dispose()
    if task is not None:
        await task
    after = len(root.logger.exporters)
    # the console exporter is owned by the console fiber and removed on unload;
    # the root-owned buffer exporter remains.
    assert after == before - 1, (before, after)


def test_console_logger_unloads_with_fiber():
    run(_console_logger_unloads_with_fiber())
