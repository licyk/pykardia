# Guest 开发与部署

[文档首页](README.md)

Guest 是由宿主加载的独立插件产物。Python guest 可以导出为进程、动态库或 WASM，
通过 Host 提供服务、记录日志和调用其他 guest。

## 选择产物

| 产物 | 宿主接口 | 部署条件 |
|---|---|---|
| 可执行进程 | `ProcessSource` | 需要导出时指定的 Python 解释器；源码和 SDK 已打包 |
| 动态库 | `DylibSource` | 需要匹配的共享 libpython、标准库及 CFFI backend |
| WASM | `WasmSource` | 解释器静态嵌入，宿主无需安装 Python；guest 不具备 OS 能力 |

CLI 自动支持 `.wasm/.wat` 和动态库。进程 guest 使用库 API 加载。
以下构建与运行步骤推荐在 Linux x86_64 上执行；WASM 导出需要 Python 3.11。

## 编写 guest

一个 guest 实现需要配置类型、metadata 和 apply；需要提供方法调用时实现 call，
需要释放资源时实现 dispose。示例：

```python
from dataclasses import dataclass
from typing_extensions import Self

from pykardia.guest import Error, Guest, Host, JsonValue, Level, Meta, Request, serve


@dataclass
class CounterConfig:
    """The initial value of one guest instance."""

    initial: int = 0


class Counter(Guest[CounterConfig]):
    """Expose a counter as a callable guest service."""

    Config = CounterConfig

    def __init__(self, value: int) -> None:
        """Initialize one independent counter."""
        self.value = value

    @staticmethod
    def meta() -> Meta:
        """Declare the plugin and its provided service."""
        return Meta.plugin("counter").provide("counter")

    @classmethod
    def apply(cls, host: Host, config: CounterConfig, imports: dict[str, JsonValue]) -> Self:
        """Publish the initial view and create the plugin instance."""
        host.provide("counter", {"initial": config.initial})
        return cls(config.initial)

    def call(self, host: Host, request: Request) -> JsonValue:
        """Increment the counter or return an explicit guest error."""
        if request.method != "increment":
            return super().call(host, request)
        if type(request.args) is not int:
            raise Error("increment expects an integer")
        self.value += request.args
        return self.value

    def dispose(self, host: Host) -> None:
        """Report cleanup through the host logger."""
        host.log(Level.Info, "counter disposed")


if __name__ == "__main__":
    serve(Counter)
```

保存为 `counter.py`，或直接使用仓库中的 [guest_counter.py](../examples/guest_counter.py)。
宿主会为每个插件实例建立独立状态；metadata 的读取不应依赖 apply 或访问宿主服务。

## 导出并加载进程 guest

在源码目录执行：

```sh
python -m pykardia.guest.export process examples/guest_counter.py Counter /tmp/pykardia-example-counter
```

此产物是可执行 zipapp，包含 SDK 和用户模块。部署时保留其 shebang 指向的 Python
解释器。下面的宿主程序会输出 `12`：

```python
import asyncio
from pathlib import Path

from pykardia import Context
from pykardia.hmr import GuestService, ModuleGraph, ProcessSource


async def main() -> None:
    """Load an exported process guest and invoke its counter service."""
    graph = ModuleGraph(ProcessSource())
    root = Context()
    try:
        path = Path("/tmp/pykardia-example-counter").resolve()
        definition = graph.import_(str(path)).plugin_def
        assert definition is not None
        fiber = root.plugin_def(definition, {"initial": 10})
        await fiber.settled()
        if error := fiber.error():
            raise error
        counter = root.get_as("counter", GuestService).arc()
        print(counter.call("increment", 2))
    finally:
        await root.fiber().dispose()


asyncio.run(main())
```

`GuestService.value()` 返回 provide 时发布的 JSON 视图，并不自动随实例字段改变。
`call(method, args)` 同步执行 guest 方法，返回 JSON 值或抛出错误。

## 导出动态库

安装导出依赖，并准备 C 编译器、Python 开发头文件和对应的共享 libpython：

```sh
python -m pip install '.[export]'
python -m pykardia.guest.export dylib examples/guest_counter.py Counter /tmp/pykardia-example-counter.so
```

加载时将前面示例的 `ProcessSource()` 换为 `DylibSource()`，路径改为生成的库。
在 CLI 配置中也可将 `name` 设为该库的路径。

库中已经嵌入用户源代码，但目标机器仍需提供匹配的 Python 运行时、标准库和
`_cffi_backend`。从非 Python 宿主加载时，可通过 `PYTHONHOME` 指定该运行时目录。

## 导出 WASM

从仓库源码目录准备 CPython 3.11 和 WASI SDK 构建工具链，需要 make 和可用的主机
C 编译器。首次执行会下载并编译工具链，可能耗时较长：

```sh
python scripts/build_wasi.py /tmp/pykardia-wasi
python -m pykardia.guest.export wasm examples/guest_counter.py Counter /tmp/pykardia-example-counter.wasm \
  --python-source /tmp/pykardia-wasi/Python-3.11.12 \
  --python-build /tmp/pykardia-wasi/build \
  --wasi-sdk /tmp/pykardia-wasi/wasi-sdk-25.0-x86_64-linux
```

用 `WasmSource()` 加载该文件，或将路径写入 CLI 的插件条目。
构建时的 Python 必须为 3.11，宿主运行时不需要 CPython 或 WASI SDK。

WASM guest 只有宿主的日志和服务能力，没有文件系统、网络、时钟或系统熵源，
也不能直接加载任意原生 Python 扩展。需要这些能力的插件应选择进程或动态库。

## 打包额外模块

三种导出都接受 `--module-root DIR`，将该目录下的 Python 模块和包一并打包：

```sh
python -m pykardia.guest.export process examples/guest_counter.py Counter /tmp/pykardia-example-counter \
  --module-root ./my_modules
```

目录结构应与导入名一致，例如 `my_modules/helpers.py` 对应 `import helpers`。
这不是自动安装第三方依赖的功能。WASM 还接受可重复的 `--module module.name`，
用于添加额外纯 Python 标准库模块；其依赖也需明确包含。

## 与宿主交互

| 接口 | 用途 |
|---|---|
| `Meta.plugin(name).inject(key).provide(key)` | 声明依赖和提供的服务 |
| `.dep(path)` | 声明静态模块依赖；导出的 JSON 值通过 apply 的 imports 传入 |
| `Meta.value(value)` | 导出 JSON 数据而非插件；null 表示没有 value 导出 |
| `host.provide(key, value)` | 提供可调用服务，并发布其 JSON 视图 |
| `host.view(key)` | 读取已声明依赖的 JSON 视图 |
| `host.invoke(key, method, args)` | 同步调用另一 guest 的服务 |
| `host.log(level, text)` | 发送宿主日志 |
| `host.cancelled()` | 宿主是否已取消正在运行的导出；耗时的调用可据此提前结束（仅 `kardia-guest/2` 进程 guest） |

显式 `pykardia.guest.Error` 将消息返回给宿主；未捕获异常转为 guest panic 错误。
同一实例的直接或间接重入会被拒绝。进程 guest 的 stdout 是协议通道，`serve()` 会将
普通 Python print 转到 stderr；不要直接向原始 stdout 文件描述符写入业务数据。

进程协议的 null view 会产生 JSON EOF 错误；需要表达空值时可提供包含 null 字段的对象，
如 `{"value": null}`。动态库/WASM 的 null view 可以直接返回 null。

产物保留许可证声明。与其他宿主互通时使用 `cordis_*` 入口和 `cordis` WASM imports；
自行实现宿主需要匹配这些 ABI，普通应用使用上述 Source 即可。

## 进程宿主与协议

`ProcessSource` 把模块路径当作程序运行，不带参数。需要参数、环境变量、工作目录、
自定义进程创建、超时或帧大小上限时，使用 `ProcessGuest`：

```python
from pykardia.hmr import GuestCommand, GuestMeta, ProcessGuest

guest = ProcessGuest(
    GuestCommand("python3").with_args("plugin.py").with_env("PLUGIN_ROOT", root).with_cwd(data),
    spawner=MySpawner(),   # 默认 StdSpawner
    timeout=30,            # 调用者未设置截止时间时，每次导出的时限（秒）
    frame_limit=1 << 20,   # guest 单行消息的最大字节数（默认 16 MiB）
)
# 元数据由宿主给出：构建插件时不启动进程；进程启动时必须声明相同的元数据
definition = guest.plugin(GuestMeta("jira", [], ["jira"]))
# 或像 ProcessSource 一样从 guest 读取元数据
module = guest.module("plugins/jira")
```

- `Spawner.spawn(command)` 返回 `GuestProcess(stdin, stdout, child, stderr)`；`child` 需具备
  `subprocess.Popen` 的 `pid`、`kill`、`poll`、`wait`。宿主可借此把进程交给自己的进程管理器。
- `GuestService.call_timeout(method, args, timeout)` 超时后杀死进程，之后的调用失败；
  `await service.call_async(method, args, timeout)` 在事件循环之外执行，取消该任务即取消调用。
  桥接插件的 `apply` 和 `dispose` 同样不阻塞事件循环；未设置超时时 `dispose` 限定为 5 秒。
- stderr 不再继承到宿主终端：最后 16 KiB 保存在退出报告中，每行也以 debug 级别记录。
- 进程在纤程释放之前结束时，`GuestHandle`（`service.handle()` 或 `GuestStarted` 事件）给出
  `GuestExit(status, cause, reason, stderr_tail)`；桥接插件发出 `GuestExited` 事件，并以
  `Fiber.fail` 使纤程失败、撤回其服务。`fiber.update(config)` 会启动新进程。
  `handle.ping(timeout)` 检查 `kardia-guest/2` guest 是否仍在应答。

协议有两个版本。宿主的第一行以导出形式提供 `kardia-guest/2`：
`{"export":"hello","input":{"protocol":"kardia-guest/2"}}`。版本 2 的 guest 回复
`{"ready":"kardia-guest/2"}`；版本 1 的 guest 像对待任何未知导出一样回复 `{"done":1}`，
宿主随后按版本 1 通信。版本 2 中每次导出带 `id`，相关消息都带同一 `id`；宿主还可发送
`{"cancel": id}`（guest 可提前结束，结果被丢弃）和 `{"ping": n}`（即使有导出在运行也回复
`{"pong": n}`）。`serve()` 两个版本都支持，与 Rust 的 `kardia-guest` 和 `kardia-hmr` 一致。
