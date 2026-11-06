# 配置应用

[文档首页](README.md)

配置文件是插件条目的列表，支持 YAML 和 JSON。CLI 加载配置文件、监听后续修改，
并安装日志、文件监听、loader 和 include 所需的基础服务。

## 插件条目

下面的配置可以作为 `pykardia.yml` 使用：

```yaml
- id: timer
  name: timer
- id: log
  name: logger-console
  config:
    colors: false
    showTime: "hh:mm:ss "
    levels:
      app: 3
```

运行 `pykardia pykardia.yml`。没有需要主动输出内容的插件时，应用会继续等待配置修改。

| 字段 | 作用 |
|---|---|
| `id` | 条目标识，可省略后自动生成；手动指定时不能含 `:` |
| `name` | 内置插件名、注册的插件名或 guest 文件路径 |
| `config` | 传给插件的配置；省略时为 null |
| `disabled` | 是否停用条目，默认启用；也可使用表达式 |
| `group` | 为 true 时，`config` 是子条目列表 |
| `inject` | 额外依赖名列表，或依赖名到元数据的对象 |
| `isolate` | 为指定服务建立私有或共享隔离范围 |
| `intercept` | 为指定服务附加访问元数据 |

条目会保留未知字段。建议为需要更新、移动或打补丁的条目显式设置稳定的 `id`。

## 内置插件和模块路径

`pykardia.catalog()` 注册 `group`、`timer`、`logger-console`、`fs`、`include`。
CLI 另外注册 `hmr`。内置名称优先于同名文件路径。

CLI 将 `.wasm` 和 `.wat` 文件作为 WASM guest 加载，其他文件扩展名交给动态库加载器。
配置中的 Python 文件不会自动导入为 Python 插件。要使用自定义 Python 函数插件，
需要通过下面的库 API 注册，或先[导出为 guest](guests.md)。

CLI 的相对 guest 模块路径以启动时指定的配置文件目录为基准。
嵌套 include 的 `path` 则相对于包含它的配置目录解析。

## 在 Python 中注册插件

下面的程序将一个 Python 函数注册为配置中的 `echo` 插件：

```python
import asyncio

from pykardia import Context, JsonValue, catalog, plugin
from pykardia.loader import Loader


@plugin
async def echo(ctx: Context, config: JsonValue) -> None:
    """Display the current entry configuration."""
    print(config)


async def main() -> None:
    """Create and update a real loader entry using a registered Python plugin."""
    root = Context()
    try:
        plugins = catalog().register_def("echo", echo)
        await root.plugin_def(Loader.plugin(plugins)).settled()
        loader = root.get(Loader).arc()
        await loader.create({"id": "message", "name": "echo", "config": "hello"})
        await loader.settled()
        await loader.update("message", {"config": "updated"})
        await loader.settled()
    finally:
        await root.fiber().dispose()


asyncio.run(main())
```

`loader.update()` 接收条目字段的浅层补丁；`config` 会整体替换，字段值为 null 表示
删除该条目字段。使用 `loader.resolve(id)` 获取 `EntryHandle`，通过 `inner_fiber()`
查询实际插件状态；`loader.settled()` 等待当前条目操作与相关插件静止。

## 分组和隔离

```yaml
- id: workers
  name: group
  group: true
  isolate:
    cache: true
  config:
    - id: clock
      name: timer
```

`isolate: {cache: true}` 为该组创建私有的 cache 服务范围，组内提供者和消费者可以共享
它，但不会误用外部同名服务。使用同一个字符串标签，例如 `cache: team-a`，可以让多个
条目共享一个托管隔离范围。隔离只改变服务解析，不会自动创建服务。

`intercept: {http: {proxy: "..."}}` 可以附加服务访问元数据；只有读取这些元数据的服务
才会受到影响。它不是通用的网络代理开关。

## include 与写回

```yaml
- id: extra
  name: include
  config:
    path: extra.yml
    watch: true
    initial: []
    patches:
      - id: clock
        disabled: true
```

`path` 指向另一个条目列表。`initial` 提供文件不存在时的初始内容，`watch` 默认 false；
CLI 创建的顶层 include 会启用监听。上例的补丁停用被包含文件中 id 为 clock 的条目。

补丁通过 `id` 定位，可覆盖字段；可用 `insert` 列表插入新条目。若补丁指定 `name`，
它会作为目标插件名的匹配条件。运行时修改由 include 路由回对应文件或父 include
的 patches；文件与内存对同一字段发生冲突时优先采用文件值。

通过 `Include.of(tree)` 取得文件管理器后，可调用 `refresh()` 重新读取，调用 `flush()`
等待写回。完整可运行示例见 [config_sync.py](../examples/config_sync.py)。
保存会重新序列化 YAML/JSON，不保证保留原注释和排版；退出时会先 flush 已排队的用户修改。

## 表达式

YAML 的 `!!js` 标签表示配置表达式，JSON 中对应 `{"__jsExpr": "表达式"}`。
例如，已有提供 JSON 视图的 settings 服务时，可以声明并使用它：

```yaml
- id: worker
  name: my-worker
  inject: [settings]
  disabled: !!js "!settings.enabled"
  config:
    endpoint: !!js "settings.endpoint"
```

这里的 `my-worker` 和 settings 需要由应用注册或提供。
表达式支持有限的 JS 风格运算、成员访问、条件和短路，不支持任意 Python/JavaScript
程序或函数调用。服务访问仍受声明和隔离约束。
表达式错误会附带配置字段和源码位置信息，处理方法见[故障排查](troubleshooting.md)。
