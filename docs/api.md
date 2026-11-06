# 常用 API 参考

[文档首页](README.md)

此页用于快速查找应用常用入口。完整操作示例见对应主题指南。

## 模块入口

| 导入路径 | 主要类型与函数 | 使用指南 |
|---|---|---|
| `pykardia` | Context、PluginDef、Plugin、Fiber、Service、Svc、Injected、plugin、service | [插件与服务](plugins.md) |
| `pykardia.prelude` | 常用核心接口，以及 Loader、Timer、TimerPlugin | [快速开始](getting-started.md) |
| `pykardia.timer` | TimerExt、Ticks、Scheduled、Disposed | [常用服务](resources.md) |
| `pykardia.fs` | Fs、FsPlugin、FsExt、WatchOptions、Watcher | [常用服务](resources.md) |
| `pykardia.logger_console` | ConsoleConfig、ConsoleExporter、ConsolePlugin | [日志](resources.md#日志) |
| `pykardia.loader` | Catalog、Resolver、Loader、EntryHandle、TreeHandle | [配置应用](configuration.md) |
| `pykardia.include` | Include、IncludeConfig、IncludePlugin | [配置应用](configuration.md#include-与写回) |
| `pykardia.hmr` | ModuleGraph、ModuleSource、ModuleCode、Exports、Hmr、HmrExt、GuestService | [热更新](hot-reload.md) |
| `pykardia.guest` | Guest、Host、Meta、Request、Error、serve | [Guest 开发](guests.md) |

## Context 与插件定义

| 调用 | 返回值或作用 |
|---|---|
| `Context()` | 新建应用根上下文 |
| `ctx.plugin_def(definition, config=None)` | 创建子插件，返回 Fiber；config 为 JSON 值 |
| `ctx.plugin(PluginClass, config)` | 用已知配置类型创建类式插件 |
| `ctx.plugin_value(PluginClass, config=None)` | 用 JSON 配置创建类式插件 |
| `PluginDef.new(async_function)` | 从 `(Context, JsonValue)` 异步函数建立定义 |
| `definition.inject(names)` | 返回带依赖声明的新定义 |
| `definition.provides(names)` | 返回带提供服务描述的新定义 |
| `ctx.registry().replace(old, new)` | 替换定义，保持已存在 fiber 的身份 |

装饰器声明的插件函数接收 `Context` 或 `Injected[tuple[ServiceType, ...]]`，
可以再接收一个带注解的配置参数。`@plugin` 返回 PluginDef，不是供应用直接调用的协程函数。

## Fiber 与服务

| 调用 | 用途 |
|---|---|
| `fiber.state()` / `fiber.error()` | 查看状态和当前加载错误 |
| `await fiber.settled()` | 等待当前转换静止；Pending 也可静止 |
| `await fiber.update(config)` | 更新 JSON 源配置并等待处理 |
| `await fiber.restart()` | 请求重载当前实例；不清除 Failed 错误 |
| `await fiber.dispose()` | 释放子插件并等待清理 |
| `ctx.provide(service)` | 按 Service 的 NAME 提供实例，返回 EffectHandle |
| `ctx.provide_as(key, value)` | 按名称提供对象 |
| `ctx.provide_serialized_as(key, value, serialize=None)` | 提供对象及动态 JSON 视图 |
| `ctx.get(ServiceType)` / `ctx.get_as(key, ValueType)` | 获取带调用者信息的 Svc |
| `ctx.view(key)` | 获取服务的 JSON 视图 |
| `ctx.live(ServiceType)` | 查询当前可用的服务，无结果时返回 None |
| `svc.arc()` / `svc.caller()` | 获取实际对象或调用者 Context |

普通插件需要声明所访问的外部依赖；根 Context 可直接读取可用服务。
等待操作可能抛出错误，使用 try/finally 保证应用最终清理。

## 资源、事件与日志

| 调用 | 用途 |
|---|---|
| `ctx.on_dispose(callback)` | 注册同步清理 |
| `ctx.defer(label, Disposer.future(factory))` | 注册异步清理 |
| `ctx.effect_sync(label, setup)` / `ctx.effect(label, setup)` | 将创建与逆操作归为一个 effect |
| `await handle.dispose()` | 提前释放 effect 并等待完成 |
| `ctx.on(EventType, listener)` / `ctx.once(...)` | 注册同步事件监听器 |
| `ctx.on_async(EventType, listener)` | 注册异步事件监听器 |
| `ctx.emit(event)` / `await ctx.parallel(event)` | 派发事件，或等待全部监听工作 |
| `ctx.bail(event)` / `await ctx.serial(event)` | 获取首个有效监听结果 |
| `ctx.logger()` / `ctx.logger_named(name)` | 获取日志对象 |

## Loader 与 Include

`catalog()` 创建内置插件目录；`register_def(name, definition)` 加入 Python 插件定义。
`Loader.plugin(resolver)` 返回可用 `ctx.plugin_def()` 安装的 loader 定义。

| 调用 | 用途 |
|---|---|
| `await loader.create(options)` | 创建条目 |
| `await loader.update(id, patch)` | 浅层更新条目字段 |
| `loader.remove(id)` | 发起删除；之后可等待 loader.settled() |
| `await loader.settled()` | 等待加载器当前操作和插件转换 |
| `loader.resolve(id)` | 获取条目句柄 |
| `entry.inner_fiber()` | 取得插件 fiber；尚未创建时为 None |
| `entry.subtree()` | 获取条目拥有的子树；没有时为 None |
| `Include.of(tree)` | 获取该树的 include 管理器；非 include 树返回 None |
| `await include.refresh()` / `await include.flush()` | 重新读取文件 / 等待写回 |

## 错误处理

`ValidationError` 表示配置解码失败；`Undeclared`、`NotProvided`、`TypeMismatch` 和
`Inactive` 用于服务访问问题。`PyKardiaError` 是非法 effect/提供者操作的一组错误基类，
并不是所有框架异常的统一基类。

`pykardia.Error` 是保留底层异常身份的包装器，可调用 `as_std()` 或 `downcast_ref(Type)`。
`pykardia.guest.Error` 用于 guest 主动向宿主报告业务错误，导入时注意区分。
需要定位具体故障时参阅[故障排查](troubleshooting.md)。
