"""HMR — the classify / detect / transactional-reload algorithms (Algorithms 8–10).

The file-watch loop is environment-dependent; these tests exercise the pure
graph algorithms with synthetic import graphs.
"""
import asyncio
import sys
import types

from cordis import Context
from cordis.loader import HmrService, RestartRequired, LoaderService


def run(coro):
    return asyncio.run(coro)


def _install(name, imports=()):
    module = types.ModuleType(name)
    for dep in imports:
        module.__dict__[dep] = sys.modules.get(dep)
    sys.modules[name] = module
    return module


async def _classify_accepted_when_dep_accepted():
    root = Context()
    hmr = HmrService(root, {})
    # graph: app -> lib; lib changed -> app accepted (depends on accepted lib)
    _install("hmr_lib")
    _install("hmr_app", imports=("hmr_lib",))
    accepted, declined = hmr.classify({"hmr_lib"}, externals=set())
    assert "hmr_lib" in accepted | declined
    # once lib is accepted, app (which imports lib) becomes accepted too
    accepted, _ = hmr.classify({"hmr_lib", "hmr_app"}, externals=set())
    assert "hmr_app" in accepted


async def _classify_declined_for_external():
    root = Context()
    hmr = HmrService(root, {})
    _install("hmr_external")
    _accepted, declined = hmr.classify({"hmr_external"}, externals={"hmr_external"})
    assert "hmr_external" in declined


async def _detect_stale_entry():
    root = Context()
    hmr = HmrService(root, {})
    _install("hmr_dep")

    class FakeEntry:
        name = "hmr_dep"
        ctx = root
        fiber = None
        config = {}
        runtime = None

    stale = hmr.detect(accepted={"hmr_dep"}, entries=[FakeEntry()])
    assert len(stale) == 1


def test_classify_accepted_when_dep_accepted():
    run(_classify_accepted_when_dep_accepted())


def test_classify_declined_for_external():
    run(_classify_declined_for_external())


def test_detect_stale_entry():
    run(_detect_stale_entry())


async def _ignored_directory_is_skipped():
    import os
    import tempfile

    root = Context()
    hmr = HmrService(root, {"ignored": ["**/node_modules", "**/.*"]})
    with tempfile.TemporaryDirectory() as d:
        nm = os.path.join(d, "node_modules", "pkg")
        os.makedirs(nm)
        open(os.path.join(nm, "x.py"), "w").close()
        open(os.path.join(d, "src.py"), "w").close()
        hmr._scan([d])  # record mtimes (only non-ignored)
        os.utime(os.path.join(nm, "x.py"), (1000, 1000))
        os.utime(os.path.join(d, "src.py"), (1000, 1000))
        changed = hmr._scan([d])
        assert "src" in changed, changed
        assert not any("node_modules" in m for m in changed), changed


def _file_to_module_handles_init_and_packages():
    import os
    import tempfile

    root = Context()
    hmr = HmrService(root, {})
    with tempfile.TemporaryDirectory() as d:
        pkg = os.path.join(d, "pkg")
        os.makedirs(pkg)
        init_path = os.path.join(pkg, "__init__.py")
        sub = os.path.join(pkg, "sub.py")
        open(init_path, "w").close()
        open(sub, "w").close()
        assert hmr._file_to_module(init_path, d) == "pkg"
        assert hmr._file_to_module(sub, d) == "pkg.sub"


def test_ignored_directory_is_skipped():
    run(_ignored_directory_is_skipped())


def test_file_to_module_handles_init_and_packages():
    _file_to_module_handles_init_and_packages()


async def _declined_change_path_raises_restart():
    # _on_change with an external (declined) module calls loader.exit() -> RestartRequired
    root = Context()
    await root.plugin(LoaderService, {})
    hmr = HmrService(root, {})
    hmr._externals = {"framework_dep"}
    raised = False
    try:
        await hmr._on_change({"framework_dep"})
    except RestartRequired:
        raised = True
    assert raised


async def _poll_surfaces_restart_to_host():
    # the fire-and-forget watch task must surface RestartRequired to the host
    # via restart_error / restart_event, not die as an unretrieved exception.
    root = Context()
    await root.plugin(LoaderService, {})
    hmr = HmrService(root, {"debounce": 1})
    hmr._externals = {"framework_dep"}
    hmr._scan = lambda roots: {"framework_dep"}  # force a declined change
    hmr.watch()
    await asyncio.sleep(0.05)
    assert hmr.restart_event.is_set()
    assert isinstance(hmr.restart_error, RestartRequired)


def test_declined_change_path_raises_restart():
    run(_declined_change_path_raises_restart())


def test_poll_surfaces_restart_to_host():
    run(_poll_surfaces_restart_to_host())
