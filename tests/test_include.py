"""Include — YAML/JSON file tree, ``!py`` interpolation, patches."""
import asyncio
import os
import sys
import tempfile
import types

from cordis import Context
from cordis.loader import LoaderService, include


def run(coro):
    return asyncio.run(coro)


def _install_fake_module(name, plugin):
    module = types.ModuleType(name)
    module.plugin = plugin
    sys.modules[name] = module
    return module


async def _reads_yaml_and_creates_entries():
    root = Context()
    await root.plugin(LoaderService, {})
    fired = []

    def plugin(ctx, config):
        fired.append(config.get("enabled"))

    _install_fake_module("cordis_test_inc_plugin", plugin)

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "cordis.yml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(
                "- id: a\n"
                "  name: cordis_test_inc_plugin\n"
                "  config:\n"
                "    enabled: !py '1 < 2'\n"
            )
        await root.plugin(include, {"path": path})
        await root.loader.await_()
    assert fired == [True], fired


async def _patches_override():
    root = Context()
    await root.plugin(LoaderService, {})
    seen = []

    def plugin(ctx, config):
        seen.append(config.get("v"))

    _install_fake_module("cordis_test_patch_plugin", plugin)

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "cordis.yml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("- id: a\n  name: cordis_test_patch_plugin\n  config: {v: 1}\n")
        await root.plugin(
            include,
            {"path": path, "patches": [{"id": "a", "config": {"v": 99}}]},
        )
        await root.loader.await_()
    assert seen == [99]


def test_reads_yaml_and_creates_entries():
    run(_reads_yaml_and_creates_entries())


def test_patches_override():
    run(_patches_override())
