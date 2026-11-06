# 计时器、文件监听、事件与日志

[文档首页](README.md)

## 计时器

先安装 `TimerPlugin`，再通过 `TimerExt(ctx.get(Timer))` 或 `ctx.timer()` 使用计时器。
时间参数使用 `datetime.timedelta`：

```python
import asyncio
from datetime import timedelta

from pykardia import Context
from pykardia.timer import TimerPlugin


async def main() -> None:
    """Run a timeout and a tick reader, then release all timer resources."""
    root = Context()
    try:
        await root.plugin(TimerPlugin, None).settled()
        timer = root.timer()
        timer.timeout(timedelta(milliseconds=10), lambda: print("timeout"))
        await timer.sleep(timedelta(milliseconds=30))
        with timer.ticks(timedelta(milliseconds=10)) as ticks:
            await ticks.tick()
            print("tick")
    finally:
        await root.fiber().dispose()


asyncio.run(main())
```

| 方法 | 行为 |
|---|---|
| `timeout(delay, callback)` | 延迟执行一次，返回可释放的 effect handle |
| `sleep(delay)` | 等待一段时间；所属实例释放时抛出 `Disposed` |
| `interval(delay, callback)` | 周期调用；第一次在完整周期后执行 |
| `ticks(delay)` | 返回可逐次等待的 Ticks；用 `close()` 或 with 结束 |
| `debounce(delay, callback)` | 返回 Scheduled；多次 `.call(value)` 只保留最后一次等待 |
| `throttle(delay, trailing, callback)` | 限制调用频率；trailing 决定是否执行窗口末尾调用 |

回调是同步函数，避免在其中阻塞事件循环。interval 和 ticks 的周期必须大于零。
在插件中使用 timer 时应声明对 Timer 的依赖；生成的资源属于访问服务的调用者实例，
调用者卸载时会自动取消，不会一直存活到 TimerPlugin 自身退出。

## 文件监听

`FsExt` 可以监听文件或递归目录。下面的程序等待真实文件变化后退出：

```python
import asyncio
import tempfile
from pathlib import Path

from pykardia import Context
from pykardia.fs import Fs, FsExt, FsPlugin, WatchOptions


async def main() -> None:
    """Watch a temporary file and release the native watcher before deleting it."""
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "message.txt"
        path.write_text("before", encoding="utf-8")
        root = Context()
        try:
            await root.plugin(FsPlugin, None).settled()
            watcher = FsExt(root.get(Fs)).watch(directory, WatchOptions(filter=["*.txt"]))
            ready, changed = asyncio.Event(), asyncio.Event()
            watcher.on_ready(ready.set)
            watcher.on_change(lambda _: changed.set())
            await asyncio.wait_for(ready.wait(), 5)
            path.write_text("after", encoding="utf-8")
            await asyncio.wait_for(changed.wait(), 5)
            print("file changed")
        finally:
            await root.fiber().dispose()


asyncio.run(main())
```

过滤器按顺序应用，最后匹配的规则决定是否包含；`!` 表示排除，例如
`["**/*.py", "!**/__pycache__/**"]`。文件变更回调接收绝对 Path。
`on_ready` 在注册后异步通知，`on_error` 接收原生监听异常。

监听覆盖创建和修改，不承诺将每种删除事件都报告为 change，也不保证操作系统通知的
数量。需要安静窗口合并变更时可使用 [HMR](hot-reload.md) 的 debounce。
可以 `await watcher.dispose()` 提前停止；否则随所属实例清理。

## 自定义事件

事件按类型分发，监听器也会随其所属 Context 清理：

```python
import asyncio
from dataclasses import dataclass

from pykardia import Context, Event


@dataclass
class Changed(Event[None]):
    """Carry one application update."""

    text: str


async def main() -> None:
    """Publish an event through a listener owned by the root context."""
    root = Context()
    try:
        root.on(Changed, lambda event: print(event.text))
        root.emit(Changed("updated"))
    finally:
        await root.fiber().dispose()


asyncio.run(main())
```

`on` 注册同步监听器，`once` 只接收一次，`on_async` 注册异步监听器。
`emit` 派发并启动异步监听工作；需要等待所有监听器结束时用 `await ctx.parallel(event)`。
需要首个有效返回值时使用 `bail`（同步）或 `serial`（顺序等待）。
可订阅 `StatusChanged`、`ServiceChanged`、`ConfigCommitted` 等框架事件。

## 日志

插件内使用 `ctx.logger().info(text)`，也可调用 `error`、`warn`、`debug`。
库模式下可通过 `root.add_exporter(ConsoleExporter(ConsoleConfig()))` 显示控制台日志，
这两个类型从 `pykardia.logger_console` 导入。CLI 默认已有 console exporter。

`logger-console` 插件用于为应用配置额外的 exporter。常用配置：

| 字段 | 含义 |
|---|---|
| `colors` | false 关闭颜色，true 开启；也接受颜色能力数值 |
| `levels` | 日志名称到级别的映射：0 error、1 warn、2 info、3 debug |
| `showTime` | 时间模板，默认 `yyyy-MM-dd hh:mm:ss ` |
| `showDiff` | 是否显示日志时间差，默认 false |
| `maxLength` | 消息长度上限，默认 10240 |
| `label` | 标签的 width、margin、align（left/right） |

`ctx.log_buffer()` 返回保留的日志记录，适合诊断加载失败；
`ctx.set_log_buffer_size(size)` 设置缓冲大小。
