# cordis (Python port)

A faithful, runtime-identical Python port of the vendored **Cordis v4** framework
(`vendor/cordis/`), as formalized in the paper *"A Programming Paradigm for
Spatiotemporal Composability"*. Cordis is a meta-framework for **spatiotemporal
composability** — the ability to load, unload, and reconfigure components at
runtime while (a) completely reverting each component's side effects
(**temporal composability**) and (b) declaring and reactively managing
inter-component dependencies (**spatial composability**).

This package replicates **all** Cordis mechanisms in Python 3.10+, using
`asyncio` as the runtime.

## Quick start

```python
import asyncio
from cordis import Context, Service


class Counter(Service):
    provide = "counter"

    def __init__(self, ctx, config=None):
        super().__init__(ctx)
        self.value = 0

    def next(self):
        self.value += 1
        return self.value


def greeter(ctx, config):
    return ctx.on("app/ready", lambda msg: ctx.logger.info("%s #%d", msg, ctx.counter.next()))


greeter.inject = ["counter"]  # coeffect specification: wait for `counter`


async def main():
    root = Context()
    await root.plugin(Counter)          # provides `ctx.counter`
    await root.plugin(greeter)          # injects `counter` -> reactive activation
    root.emit("app/ready", "started")
    await root.fiber.dispose()


asyncio.run(main())
```

## The five ideas (and where they live)

| Cordis idea | Paper | Implementation |
| --- | --- | --- |
| A plugin is an object that implements `Service` | component `(σ, π, φ)` | `service.py` (`Service`), `registry.py` (`Plugin` shapes) |
| A context is a repository of services | unified context Γ | `context.py` (`Context` proxy), `reflect.py` (`store`/`props`) |
| Declare dependencies via `inject` | coeffect specification σ | `registry.py` (`Inject`), `fiber.py` (`Fiber.inject`) |
| Typed events for communication | — | `events.py` (`emit`/`parallel`/`serial`/`bail`/`waterfall`) |
| Registrations are reversible effects | revertible effects `Γ → Γ × Γ^Γ` | `fiber.py` (`Fiber.effect`, `_disposables` LIFO) |

### Revertible effects (temporal composability)

`ctx.effect(execute)` runs `execute` immediately and collects the **disposers**
(inverses) it returns — a plain callable, a generator, or an async generator
yielding multiple. Disposers run in **reverse order** on disposal, recovering
the pre-composition state (Theorem 61 / Corollary 62). A fully-sync effect
disposes **synchronously**, so a later synchronous dispatch never observes a
removed registration.

### Reactive coeffects (spatial composability)

A plugin's `inject` is its **specification**. When a required service is
provided or unloaded, `ReflectService.notify` re-checks each dependent fiber
(`_check_impl`), recomputes its **epoch** — a digest of providing-fiber uids
(`':' + uid`) — and `_set_epoch` starts a `_reload` (**activating**) or
`_unload` (**deactivating**); an unchanged epoch is **neutral**. This is
Definition 26's classification, driven by the epoch (the paper's `fiber.target`).

### Unified context

`Context` is a proxy: reads of real fields walk the parent chain, while reads
of unbound names resolve a service through `ReflectService` (`internal/get`
waterfall + the fiber-chain walk). `isolate(name, label)` and
`intercept(name, config)` create scoped child contexts (realms) without
mutating the parent — the coeffect isolation and interception of Definitions
28–31.

### Component lifecycle

`Fiber` + `FiberState` (`PENDING → LOADING → ACTIVE`, `FAILED`, `UNLOADING`,
`DISPOSED`) mirror the calculus transitions: `_reload`/`_unload` (L-Begin/Iter/
Finish/Divert), `restart`/`update` (iteration, with the `internal/update`
waterfall veto), `inertia` (the in-flight transition handle), and `FAILED`
(L-Raise, per-fiber).

## Component loader, include, HMR

| Package | Port of | Mechanism |
| --- | --- | --- |
| `cordis.loader.loader` | `@cordisjs/plugin-loader` | `EntryTree`, `create`/`update`/`remove`, per-field reconciliation |
| `cordis.loader.include` | `@cordisjs/plugin-include` | YAML/JSON file tree, `!py` interpolation, patches |
| `cordis.loader.hmr` | `@cordisjs/plugin-hmr` | 3-phase HMR (classify → detect → transactional reload) over `importlib` |
| `cordis.loader.group` | `@cordisjs/plugin-group` | nested plugin groups |
| `cordis.timer` | `@cordisjs/plugin-timer` | disposal-aware `setTimeout`/`setInterval`/`debounce`/`throttle` |
| `cordis.logger_console` | `@cordisjs/plugin-logger-console` | console exporter |

## Python-specific adaptations (documented)

These preserve every runtime **mechanism**; only Python-idiom shape differs
from TypeScript:

- **Async-native.** `Fiber.inertia` is an `asyncio.Task`; `await asyncio.sleep(0)`
  is the `await Promise.resolve()` checkpoint. Cordis-Python runs inside an
  event loop (`asyncio.run`).
- **Dispatch awaitability split (faithful).** `emit`, `bail`, `waterfall` are
  **synchronous** (so `ctx.tools`-style service reads, which run the
  synchronous `internal/get` waterfall, stay synchronous — matching TS);
  `parallel`/`serial` are coroutines.
- **Dispatch context as a leading argument.** TypeScript binds the dispatch
  context as the listener's `this`; Python has no implicit `this`, so a
  listener receives it as the first argument when the event's contract passes
  one (per-event, consistent — the Python rendering of TS's bound `this`).
- **Symbols → module singletons.** `Symbol.for(...)` becomes process-unique
  sentinel objects in `symbols.py`. Python `getattr` needs string names, so
  symbol-keyed framework slots use private attributes (e.g. `_tracker`,
  `_init`, `_invoke`).
- **Traceable proxy.** TS wraps root-shared core services in a `Proxy` that
  rebinds `ctx` to the caller, so effects/listeners attribute to the correct
  fiber. Python uses a small `_Traceable`/`_Shadow` wrapper that fetches the
  unbound method from the class and binds the caller context — preserving the
  load-bearing fiber attribution.
- **`!!js` → `!py`.** Include's config-file expression interpolation evaluates
  Python expressions (with `os.environ` in scope).
- **HMR over `importlib`.** The module cache is `sys.modules`; reload is
  `importlib.reload` / re-import. File watching polls `mtime` (stdlib). Python
  module-reload caveats apply; the 3-phase algorithm is preserved.
- **`loader.exit()` raises `RestartRequired`.** TypeScript restarts the host
  process on a framework-level (declined) dependency change. Python cannot
  cleanly re-exec a live module graph, so `exit()` raises
  `cordis.loader.RestartRequired`. Because HMR's watch loop is a
  fire-and-forget task, the exception is also surfaced as
  `hmr.restart_error` / `hmr.restart_event` (an `asyncio.Event`) for the host
  to observe and re-launch — the declined path is never silent.
- **`!py` config expressions are code.** Include's `!py` tag evaluates a
  Python expression with `os.environ` in scope (the rendering of TS `!!js`).
  This is arbitrary code execution — treat config files as trusted. Also,
  write-back (`ctx._include_write()`) serializes the *evaluated* value, so a
  `!py` expression is not preserved on round-trip (the marker is lost);
  write-back is best-effort and documented as such.
- **Typing.** TypeScript's compile-time typed events and `Context` interface
  augmentation (declaration merging) have no Python equivalent; this port is
  runtime-identical and drops compile-time typing (per design).

## Layout

```
python/cordis/
  src/cordis/
    __init__.py  symbols.py  utils.py
    context.py   reflect.py  registry.py  fiber.py  service.py
    events.py    logger.py
    loader/{loader,include,hmr,group}.py
    timer/__init__.py  logger_console/__init__.py
  tests/test_{effect,coeffect,events,fiber,service,loader,include,hmr,timer,logger}.py
  pyproject.toml
```

## Develop

```sh
cd python/cordis
pip install -e .[test]   # or: pip install pyyaml pytest
pytest                  # 52 tests
```

The tests double as the specification: each `test_*.py` proves one paper
invariant (LIFO recovery, reactive activation/deactivation, epoch identity,
isolation realms, waterfall short-circuit, lifecycle transitions, HMR phases).
