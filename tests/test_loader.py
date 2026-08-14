"""Loader — declarative EntryTree, create/update/remove, reconciliation."""
import asyncio
import sys
import types

from cordis import Context
from cordis.loader import LoaderService, RestartRequired


def run(coro):
    return asyncio.run(coro)


def _install_fake_module(name, plugin):
    module = types.ModuleType(name)
    module.plugin = plugin
    sys.modules[name] = module
    return module


async def _create_starts_entry():
    root = Context()
    await root.plugin(LoaderService, {})
    fired = []

    def plugin(ctx, config):
        fired.append(config.get("v"))
        return lambda: fired.append("off")

    _install_fake_module("cordis_test_plugin_a", plugin)
    eid = await root.loader.create({"id": "a", "name": "cordis_test_plugin_a", "config": {"v": 1}})
    await root.loader.await_()
    assert eid == "a"
    assert fired == [1]


async def _remove_stops_entry():
    root = Context()
    await root.plugin(LoaderService, {})
    off = []

    def plugin(ctx, config):
        return lambda: off.append("off")

    _install_fake_module("cordis_test_plugin_b", plugin)
    await root.loader.create({"id": "b", "name": "cordis_test_plugin_b"})
    await root.loader.await_()
    await root.loader.remove("b")
    assert off == ["off"]


async def _update_reconfigures():
    root = Context()
    await root.plugin(LoaderService, {})
    seen = []

    def plugin(ctx, config):
        seen.append(config.get("v"))
        return lambda: seen.append("off")

    _install_fake_module("cordis_test_plugin_c", plugin)
    await root.loader.create({"id": "c", "name": "cordis_test_plugin_c", "config": {"v": 1}})
    await root.loader.await_()
    await root.loader.update("c", {"config": {"v": 2}})
    await root.loader.await_()
    assert seen == [1, "off", 2], seen


async def _resolve_nested_id():
    root = Context()
    await root.plugin(LoaderService, {})
    await root.loader.create({"id": "grp", "group": True})
    await root.loader.create({"id": "child", "group": True}, parent="grp")
    assert root.loader.resolve("grp:child") is not None
    assert root.loader.resolve("grp:missing") is None


async def _disabled_does_not_start():
    root = Context()
    await root.plugin(LoaderService, {})
    fired = []

    def plugin(ctx, config):
        fired.append("on")

    _install_fake_module("cordis_test_plugin_d", plugin)
    await root.loader.create({"id": "d", "name": "cordis_test_plugin_d", "disabled": True})
    await root.loader.await_()
    assert fired == []


def test_create_starts_entry():
    run(_create_starts_entry())


def test_remove_stops_entry():
    run(_remove_stops_entry())


def test_update_reconfigures():
    run(_update_reconfigures())


def test_resolve_nested_id():
    run(_resolve_nested_id())


def test_disabled_does_not_start():
    run(_disabled_does_not_start())


async def _exit_raises_restart_required():
    root = Context()
    await root.plugin(LoaderService, {})
    raised = False
    try:
        root.loader.exit()
    except RestartRequired:
        raised = True
    assert raised


def test_exit_raises_restart_required():
    run(_exit_raises_restart_required())
