# 热更新

[文档首页](README.md)

HMR 重新读取发生变化的模块，并更新依赖这些模块的插件。模块导入失败时保留相关的
旧实现；独立模块的成功更新仍可完成。适合需要在运行中修改 guest 文件的应用。

## 使用配置监听

运行 `pykardia examples/pykardia.yml` 即可体验。配置中需要一个 hmr 条目：

```yaml
- id: hot
  name: hmr
  config:
    watch:
      enabled: true
      files: [plugins, '!**/.*']
      debounce: 100
- id: hello
  name: ./plugins/hello.wat
```

此例假设配置旁边存在 `plugins/hello.wat`。编辑文件内容后保存即可触发重载。
配置文件自身的变更由 include 监听；guest 模块内容的变更由 hmr 监听。

| 配置 | 默认值与含义 |
|---|---|
| `base` | 当前配置所属目录；无配置条目时从工作目录解析，可指定相对路径 |
| `watch.enabled` | false；设为 true 开启模块文件监听 |
| `watch.files` | `[".", "!**/.*", "!**/target"]`，相对 base 的过滤规则 |
| `watch.debounce` | 100，单位毫秒；收集一个安静窗口内的变化 |

监听需要 Fs 服务，CLI 已安装。库模式下需要自行安装 `FsPlugin`。
重载失败的路径会保留到下一批文件变更时重试；修复文件后保存即可再次触发。

## 在 Python 中手动触发

下面的例子加载示例 WAT，再对同一路径执行一次重载：

```python
import asyncio
from pathlib import Path

from pykardia import Context
from pykardia.hmr import Hmr, HmrExt, ModuleGraph, WasmSource


async def main() -> None:
    """Load a WASM plugin and reconcile its file through the HMR service."""
    path = Path("examples/plugins/hello.wat").resolve()
    graph = ModuleGraph(WasmSource())
    root = Context()
    try:
        await root.plugin_def(Hmr.plugin(graph)).settled()
        definition = graph.import_(str(path)).plugin_def
        assert definition is not None
        fiber = root.plugin_def(definition)
        await fiber.settled()

        async def changed_files() -> list[Path]:
            """Return the changed module paths inside the transaction."""
            return [path]

        result = await HmrExt(root.get(Hmr)).reconcile(changed_files)
        for failure in result.import_failures:
            print(failure.modules, failure.error)
        print("settled:", len(result.settled_fibers))
    finally:
        await root.fiber().dispose()


asyncio.run(main())
```

`reconcile` 的回调在事务内执行，返回待检查路径。不要从正在执行的重载事务中
再次调用 reconcile。`ModuleGraph.mark_host(id)` 可将模块标记为宿主模块，阻止 HMR 更新它。

## 理解更新结果

`ReconcileResult` 包含：

- `settled_fibers`：此次更新涉及且已等待静止的插件实例。
- `import_failures`：导入失败的模块组和错误。
- `skipped_host_modules`：被跳过的宿主模块。

导入失败与插件加载失败不同：导入失败时关联模块可恢复旧缓存；新定义已替换后，
插件函数抛出的异常会使该实例进入 Failed，不会自动回滚所有模块。
遇到这种情况应查看 `fiber.error()`，修正实现后再次更新。

内存中的普通 Python 函数可以通过 `registry.replace()` 更新定义，见
[插件与服务](plugins.md#替换实现与隔离)。HMR 不会自动监视所有已经 import 的 Python 模块。
