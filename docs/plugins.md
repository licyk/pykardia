# 插件与服务

[文档首页](README.md)

## 声明配置和依赖

用 `@plugin` 将带类型注解的异步函数声明为插件；用 `@service` 声明服务类型。
下面的程序可以直接保存并运行：

```python
import asyncio
from dataclasses import dataclass

from pykardia import Context, Injected, Service, plugin, service


@service(name="greeting")
@dataclass
class Greeting(Service):
    """A greeting shared by the provider and its consumers."""

    text: str


@dataclass
class Config:
    """Configuration accepted by the greeting provider."""

    text: str = "hello"


@plugin(provide=["greeting"])
async def provider(ctx: Context, config: Config) -> None:
    """Publish one greeting and register its cleanup."""
    ctx.provide(Greeting(config.text))
    ctx.on_dispose(lambda: print("provider released"))


@plugin
async def consumer(ctx: Injected[tuple[Greeting]]) -> None:
    """Read the greeting only after its provider becomes available."""
    greeting = ctx.get(Greeting).arc()
    print(greeting.text)
    ctx.context().on_dispose(lambda: print("consumer released"))


async def main() -> None:
    """Start the consumer, supply its dependency and update the provider."""
    root = Context()
    try:
        dependent = root.plugin_def(consumer)
        await dependent.settled()
        print(dependent.state().value)
        source = root.plugin_def(provider, {"text": "hello"})
        await source.settled()
        await dependent.settled()
        await source.update({"text": "updated"})
        await dependent.settled()
    finally:
        await root.fiber().dispose()


asyncio.run(main())
```

消费者先进入 Pending，提供者就绪后读取 `hello`。更新配置会释放旧实例并发布新服务，
消费者随之重载并读取 `updated`。消费者清理结束后，其使用的旧服务才被撤销。

配置在插件入口解码。dataclass 默认值可以省略，未知字段会被容忍；字段类型不匹配会
产生 `ValidationError`，包含问题字段的路径。不要原地修改共享配置来代替 `update()`。

## 获取服务

`ctx.get(Greeting)` 返回 `Svc[Greeting]`；调用 `.arc()` 获取实际服务对象。
普通插件声明依赖时使用 `Injected[tuple[...]]`，也可以使用
`PluginDef.new(...).inject(["service-name"])` 明确声明服务名。

对于按名称提供的数据，使用 `ctx.provide_as("name", value)` 和
`ctx.get_as("name", ValueType).arc()`。`ctx.view("name")` 读取 JSON 视图；
自定义服务需要提供可序列化视图，不能假定任意 Python 对象都可转成 JSON。

根 Context 可以直接读取可用服务。插件内部访问未声明的外部依赖会抛出
`Undeclared`；服务缺失、类型错误或实例已释放也会产生相应错误。
`ctx.live(ServiceType)` 在当前不可用时返回 `None`，适合可选的即时查询。

## 管理资源

每次插件加载都应登记对应清理动作。同步清理使用 `ctx.on_dispose(callback)`；
异步清理使用 `ctx.defer(label, Disposer.future(factory))`。例如在插件函数中：

```python
from pykardia import Context, Disposer


async def session(ctx: Context) -> None:
    """Register an asynchronous shutdown operation for this plugin episode."""
    async def close() -> None:
        """Finish asynchronous shutdown work."""
        print("session closed")

    ctx.defer("session", Disposer.future(close))
```

清理工厂应在调用时创建 awaitable，不能重复复用同一个协程对象。
使用 `effect_sync(label, setup)` 或 `effect(label, setup)` 可以把资源创建和逆操作
组织在同一个 effect 中；setup 接收 `EffectScope`，通过 `defer_fn` 或 `defer` 登记清理。
单个 effect 内部按后登记先清理执行；跨 effect 的异步清理可能并发。

退出应用时始终 `await root.fiber().dispose()`。不要依赖 Python 垃圾回收的时机。
根 fiber 的 dispose 会清理其资源并恢复根的可用状态，普通子 fiber 则进入 Disposed。

## 观察插件状态

| 状态 | 含义 | 通常如何处理 |
|---|---|---|
| Pending | 所需服务尚不可用 | 检查依赖名、提供者和隔离范围 |
| Loading | 正在执行插件加载 | 等待 `settled()` |
| Active | 当前实例已加载 | 使用服务或更新配置 |
| Unloading | 正在清理旧实例 | 等待清理和后续加载 |
| Failed | 加载失败 | 查看 `error()`，修正配置或实现 |
| Disposed | 实例已释放 | 需要时创建新实例 |

`update()`、`restart()`、`dispose()` 会在调用时开始工作，返回的对象用于等待完成。
丢弃等待对象不会取消操作。`settled()` 只观察当前状态转换，不等待未来出现的服务。

`fiber.update(new_config)` 更新源配置。运行中配置校验失败会保留旧实例；Failed 状态
可通过有效更新或替换实现恢复，单独 `restart()` 不会清除已有加载错误。

## 替换实现与隔离

`root.registry().replace(old_definition, new_definition)` 替换使用旧定义的实例，保留
它们的 fiber 身份、父级和配置。之后等待相关 fiber 的 `settled()`。
完整示例见 [provider_consumer.py](../examples/provider_consumer.py)。

`ctx.isolate("greeting")` 创建隔离该服务的子 Context；在其中创建的插件可使用独立
提供者。`ctx.intercept("service", metadata)` 为服务访问附加元数据；服务通过
`Svc.caller()` 取得调用者上下文。不要把服务定义处的 Context 当成调用者 Context。
