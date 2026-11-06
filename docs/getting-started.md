# 安装与快速开始

[文档首页](README.md)

## 安装

进入包含 `pyproject.toml` 的源码目录，创建虚拟环境并安装：

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install .
```

确认导入成功：

```sh
python -c 'import pykardia; print(pykardia.__version__)'
```

这会安装核心功能以及配置解析、文件监听和 WASM 宿主所需的依赖。
需要导出动态库 guest 时，改用 `python -m pip install '.[export]'`。

## 第一个插件

把下面的代码保存为 `app.py`，然后运行 `python app.py`：

```python
import asyncio

from pykardia import Context, JsonValue, PluginDef


async def answer(ctx: Context, config: JsonValue) -> None:
    """Publish the configured value for this plugin instance."""
    ctx.provide_as("answer", config)
    ctx.on_dispose(lambda: print("answer released"))


async def main() -> None:
    """Start a plugin, read its service and wait for cleanup before exiting."""
    root = Context()
    try:
        definition = PluginDef.new(answer).provides(["answer"])
        fiber = root.plugin_def(definition, 42)
        await fiber.settled()
        if error := fiber.error():
            raise error
        print(root.view("answer"))
    finally:
        await root.fiber().dispose()


asyncio.run(main())
```

输出为 `42` 和 `answer released`。`Context` 是应用入口，`PluginDef` 是插件定义，
`Fiber` 是运行中的插件实例。插件的函数返回后，服务仍然有效，直到实例被释放或重载。

`settled()` 等待当前转换结束；缺少依赖的插件也可能停在 Pending。
应结合 `state()` 和 `error()` 判断是否已启动，详见 [生命周期](plugins.md#观察插件状态)。

## 运行现成示例

```sh
python examples/provider_consumer.py
python examples/config_sync.py
```

第一个示例演示服务等待、配置更新、实现替换和清理顺序。
第二个示例在临时文件中演示配置文件与运行时之间的双向修改。

## 使用命令行

```sh
pykardia examples/pykardia.yml
# 等价形式：
python -m pykardia examples/pykardia.yml
```

运行后会看到 `hello from wasm`。编辑 `examples/plugins/hello.wat` 中的问候语，
保存后观察热更新；也可以将配置里的 hello 条目设为 `disabled: true`，再改回 false。
按 Ctrl-C 结束，命令会等待清理完成。

不传路径时读取当前目录的 `pykardia.yml`。初始配置错误会打印日志并返回退出码 1；
正常 Ctrl-C 退出返回 0。程序运行期间的日志写入 stdout。

CLI 可加载内置插件、WASM/WAT 和动态库文件。Python 函数插件通过库 API 注册；
独立进程 guest 通过 `ProcessSource` 加载，详见 [Guest 开发](guests.md)。

下一步：[插件与服务](plugins.md) 或 [配置应用](configuration.md)。
