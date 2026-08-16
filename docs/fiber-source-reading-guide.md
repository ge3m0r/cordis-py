# Fiber 源码阅读指南

Fiber 是 Cordis 中一次插件运行实例的生命周期容器。它不是操作系统线程，也不是 Python 协程；每次调用 `ctx.plugin(plugin, config)`，Cordis 都会为这次插件加载创建一个 Fiber。

Fiber 主要负责：

- 管理插件从等待、加载到运行、卸载和销毁的生命周期；
- 为插件创建独立的子 Context；
- 记录并撤销插件产生的事件监听、服务、定时器和子插件等副作用；
- 根据 `inject` 声明响应依赖服务的出现、消失和替换；
- 支持配置更新、重启、错误记录以及 HMR 热替换。

本文建议沿着一次 `ctx.plugin()` 调用阅读源码，而不是从 `fiber.py` 第一行顺序阅读。

## 1. 先从测试建立行为预期

首先阅读以下测试：

- `tests/test_fiber.py`：生命周期、异常、重启和配置更新；
- `tests/test_effect.py`：副作用的登记和清理；
- `tests/test_coffect.py`：依赖出现、消失或被替换后的响应行为。

可以分别运行：

```bash
pytest tests/test_fiber.py -v
pytest tests/test_effect.py -v
pytest tests/test_coffect.py -v
```

第一遍只需要形成这些行为预期：

- 缺少依赖时，Fiber 保持 `PENDING`；
- 依赖满足后，Fiber 进入 `ACTIVE`；
- 插件异常后，Fiber 进入 `FAILED`；
- `restart()` 会重新执行插件；
- `update()` 会使用新配置重启插件；
- `dispose()` 会撤销插件注册的资源；
- Fiber 本身可以被 `await`。

## 2. 从 Context 的根 Fiber 开始

阅读 `src/cordis/context.py` 中的 `Context.__init__()`：

```python
fiber = Fiber(self, {}, {}, None, lambda: [])
object.__setattr__(self, "fiber", fiber)
```

这里创建的是特殊的根 Fiber。它启动时就是 `ACTIVE`，负责持有应用级副作用和所有子插件：

```text
Context
└── root Fiber
    ├── plugin Fiber A
    ├── plugin Fiber B
    └── plugin Fiber C
```

随后阅读 `src/cordis/reflect.py` 中的 `ReflectService.__init__()`：

```python
self.mixin("fiber", ["runtime", "effect"])
self.mixin("registry", ["inject", "plugin"])
```

这解释了为什么 Context 可以直接调用 `ctx.plugin()` 和 `ctx.effect()`。它们实际分别转发到：

```python
ctx.registry.plugin(...)
ctx.fiber.effect(...)
```

## 3. 追踪 `ctx.plugin()`

下一步阅读 `src/cordis/registry.py` 中的 `RegistryService.plugin()`。

该方法的主要流程是：

```text
解析插件函数
  ↓
取得或创建 Plugin.Runtime
  ↓
解析插件的 inject 声明
  ↓
创建 Fiber
```

核心逻辑可以简化成：

```python
runtime = Plugin.Runtime(...)
inject_map = Inject.resolve(plugin.inject)
fiber = Fiber(self.ctx, config, inject_map, runtime, ...)
return fiber
```

这里要区分两个概念：

- `Plugin.Runtime` 保存插件函数本身的共享信息；
- `Fiber` 表示这个插件函数的一次实际运行。

同一个插件可以被加载多次，因此一个 Runtime 可以对应多个 Fiber。

## 4. 阅读 Fiber 构造函数

进入 `src/cordis/fiber.py` 的 `Fiber.__init__()`。第一遍先跳过文件前面的 `AsyncDisposable`，关注这些字段：

```python
self.inject = inject
self._disposables = DisposableList()
self.inertia = None
self.state = FiberState.PENDING
self._error = None
self._store = {}
```

字段含义如下：

| 字段 | 含义 |
| --- | --- |
| `inject` | 插件声明的服务依赖 |
| `_disposables` | 插件需要清理的副作用 |
| `inertia` | 当前正在执行的加载或卸载任务 |
| `state` | 当前生命周期状态 |
| `_error` | 最近一次启动异常 |
| `_store` | 当前找到的依赖服务 |
| `store` | 插件启动时使用的依赖快照 |
| `uid` | Fiber 实例标识 |

随后注意插件专属 Context 的创建：

```python
self.ctx = parent.extend({"fiber": self})
```

插件函数之后收到的 `ctx` 属于这个 Fiber，因此通过该 Context 创建的事件、服务和其他 effect 都可以归属到正确的插件实例。

## 5. 追踪无依赖插件的启动

以一个最简单的插件为例：

```python
def plugin(ctx, config):
    print("start")
    return lambda: print("stop")

fiber = await ctx.plugin(plugin)
```

Fiber 构造完成前会调用 `_refresh()`。无依赖插件会得到一个有效的空 epoch：

```python
epoch = ""
```

随后 `_set_epoch()` 发现 epoch 从 `INACTIVE` 变为有效值，于是把状态改成 `LOADING` 并创建 `_reload()` 任务。

`_reload()` 会解析配置，然后通过 `_execute()` 调用插件：

```python
self.config = self._resolve_config(self._config)
task = self._execute(self._runner)
```

启动成功后，Fiber 进入 `ACTIVE`。完整调用链是：

```text
ctx.plugin()
→ RegistryService.plugin()
→ Fiber(...)
→ _refresh()
→ _set_epoch()
→ _reload()
→ _execute()
→ plugin(ctx, config)
→ ACTIVE
```

## 6. 追踪插件清理过程

插件返回的 callable 会在 `_execute()` 中被当作 disposer 收集：

```python
runner.collect(effect)
```

它最终进入当前 Fiber 的 `_disposables`。当代码执行：

```python
await fiber.dispose()
```

Fiber 会进入 `_unload()`，清理它持有的副作用。

因此，插件函数的返回值通常不是业务结果，而是“如何撤销这个插件所做操作”的描述。通过 Cordis API 注册的事件监听器、服务、定时器和子插件也会以 effect 的形式登记到所属 Fiber，并在卸载时得到清理。

## 7. 阅读 `effect()` 和 `AsyncDisposable`

理解基本生命周期之后，再阅读：

- `Fiber.effect()`；
- `AsyncDisposable`；
- `run_disposable()`；
- `DisposableList.clear()`。

一个最简单的 effect 是：

```python
dispose = ctx.effect(
    lambda: register_and_return_unregister()
)

await dispose()
```

其运行过程为：

```text
立即执行 execute
    ↓
收集 execute 返回的清理函数
    ↓
把 AsyncDisposable 登记到当前 Fiber
    ↓
手动 dispose 或 Fiber 卸载时执行清理
```

effect 支持以下返回形式：

```python
# 单个清理函数
return cleanup

# 协程最终返回清理函数
return coroutine()

# 生成器产生多个清理函数
yield cleanup_a
yield cleanup_b

# 异步生成器产生清理函数
yield cleanup
```

第一遍可以暂时忽略 `effect_inertia` 和弱引用。它们主要解决异步清理、重复 dispose 和对象回收问题，适合第二遍深入阅读。

## 8. 阅读响应式依赖链

考虑一个声明了依赖的插件：

```python
def consumer(ctx, config):
    print(ctx.counter)

consumer.inject = ["counter"]
```

如果 `counter` 不存在，调用链是：

```text
_check_impl("counter") 找不到服务
→ _refresh()
→ epoch = INACTIVE
→ Fiber 保持 PENDING
```

当另一个插件提供该服务：

```python
ctx.provide("counter", value)
```

调用链变成：

```text
ReflectService.provide()
→ ReflectService.notify()
→ consumer Fiber._check_impl()
→ consumer Fiber._refresh()
→ consumer Fiber._set_epoch()
→ consumer Fiber._reload()
```

Fiber 使用依赖服务提供者的 UID 组成 epoch。依赖不存在时 epoch 为 `INACTIVE`；服务出现、消失或更换提供者时，epoch 发生变化，进而驱动插件加载、卸载或重载。

建议重点阅读：

- `ReflectService.provide()`；
- `ReflectService.notify()`；
- `Fiber._check_impl()`；
- `Fiber._refresh()`；
- `Fiber._set_epoch()`。

## 9. 理解 Fiber 与 HMR 的关系

HMR 的插件替换流程建立在 Fiber 的清理能力上：

```text
源文件变化
    ↓
HMR 找到受影响的 Loader Entry
    ↓
entry.fiber.dispose()
    ↓
Fiber 清理旧插件的事件、服务和定时器
    ↓
重新导入 Python 模块
    ↓
ctx.plugin(新插件)
    ↓
创建新的 Fiber
```

如果没有 Fiber，重新导入模块无法可靠清理旧插件注册的资源，容易产生重复监听、残留定时器或旧服务继续存在等问题。

## 推荐断点

使用调试器时，可以依次在以下方法设置断点：

1. `RegistryService.plugin()`；
2. `Fiber.__init__()`；
3. `Fiber._refresh()`；
4. `Fiber._set_epoch()`；
5. `Fiber._reload()`；
6. `Fiber._execute()`；
7. `Fiber.effect()`；
8. `Fiber._unload()`；
9. `ReflectService.notify()`。

调试时重点观察：

```python
fiber.state
fiber.uid
fiber.inject
fiber._store
fiber.store
fiber._runner.epoch
fiber._disposables
fiber.inertia
```

## 总体阅读顺序

```text
tests/test_fiber.py
        ↓
Context.__init__
        ↓
ReflectService.__init__ 中的 mixin
        ↓
RegistryService.plugin
        ↓
Fiber.__init__
        ↓
_refresh → _set_epoch → _reload → _execute
        ↓
effect → AsyncDisposable → _unload
        ↓
ReflectService.provide → notify
        ↓
restart / update
        ↓
最后回头看 HMR
```

第一遍阅读时，优先回答三个问题：

1. Fiber 什么时候启动插件？
2. Fiber 把清理函数保存在哪里？
3. 依赖变化为什么会导致 Fiber 重载？

理解这三个问题后，就已经掌握了 `fiber.py` 的主要设计。
