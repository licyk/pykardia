# 故障排查

[文档首页](README.md)

## 找不到模块或命令

`ModuleNotFoundError: pykardia` 通常表示运行程序的 Python 与安装依赖的 Python 不同。
在激活的虚拟环境中执行：

```sh
python -m pip install .
python -c 'import pykardia; print(pykardia.__file__)'
python -m pykardia examples/pykardia.yml
```

如果 `python -m pykardia` 可用但 `pykardia` 命令不可用，检查虚拟环境的 bin 目录是否在 PATH。
确认命令运行目录中包含所指定的配置文件；无参数时会读取当前目录的 `pykardia.yml`。

## 插件一直 Pending

Pending 表示至少一个依赖还不可用，并非已经加载成功。检查：

- 注入名与提供的服务名是否一致；`@service(name=...)` 可显式指定名称。
- 提供者是否为 Active，是否也在等待其他依赖或已经 Failed。
- 消费者是否被 isolate 隔离到另一个范围。
- 提供者是否设置了拒绝当前元数据的可用性条件。

`await fiber.settled()` 可以在 Pending 时结束。它不会一直等待未来创建的提供者。
不要用“settled 已返回”作为服务已存在的唯一判断。

## 插件加载失败或配置未生效

读取 `fiber.error()`，查看 `ctx.log_buffer()`，或给库应用安装 ConsoleExporter。
CLI 的日志在 stdout。配置错误包含字段路径；文件配置还会报告相关行列信息。

运行中传入不合法的新配置时，旧实例可能继续工作；不要忽略 update 返回的异常。
修复后重新 `update()`，或替换错误的实现。Failed 状态下仅调用 restart 不会清除旧错误。

## 找不到插件或误当成动态库

内置目录只包含注册过的插件；任意 Python 模块名不会自动解析成插件。
通过 `Catalog.register_def()` 注册 Python 函数插件。CLI 会将 `.wasm/.wat` 交给 WASM
加载器，其他文件交给动态库加载器；进程产物需要 `ProcessSource`。

## 文件监听或热更新没有触发

区分两个监听来源：include 的 `watch` 管理配置文件，hmr 的 `watch.enabled` 管理模块。
检查 base 路径、files 过滤规则和 debounce。以 `!` 开头的模式应在 YAML 中加引号。
库应用需要先安装 FsPlugin；CLI 已安装它。

原生文件事件数量由系统决定，不应等待固定次数的 change 回调。HMR 对导入失败的模块
保留旧实现并记录错误；修改文件后保存可触发重试。新实现 apply 失败则查看该 fiber 的错误。

## 配置写回暂停或格式改变

文件无法解析时，include 会暂停写回，修复文件后再刷新。
文件写入失败时变更仍保留在内存中；检查目录权限和磁盘状态后调用 `flush()` 重试。
若值由父 include 的 patches 提供，修改可能写到父配置而不是子文件。

保存会重新序列化文件，注释、引号和排版可能改变。文件内容与内存对同一字段冲突时
文件优先。应用退出前等待根释放，确保已排队的用户修改有机会 flush。

## Guest 无法启动

| 现象 | 检查项 |
|---|---|
| 进程执行失败 | 文件执行权限、shebang 指向的 Python、是否用 ProcessSource 加载 |
| 动态库找不到 libpython 或 `_cffi_backend` | 构建和运行时 Python 版本、共享库路径、CFFI backend、PYTHONHOME |
| WASM 构建失败 | 构建 Python 是否为 3.11，三个工具链参数是否指向同一次构建 |
| WASM 中导入模块失败 | 模块是否通过 module-root/module 打包；是否依赖不可用的 OS 或原生扩展 |
| `the guest panicked: ...` | guest 内未捕获异常；预期业务失败使用 guest.Error |
| `re-entrant` | 同步调用链重新进入了同一个 guest 实例 |
| 进程读取 null view 报 JSON EOF | 用包含 null 字段的对象表达空值，例如 `{"value": null}` |

构建命令和部署要求见 [Guest 开发](guests.md)。

## 退出后仍有任务或清理未完成

用 `try/finally` 确保执行 `await root.fiber().dispose()`。异步清理应登记为
`Disposer.future(factory)`，外部创建的资源必须显式登记清理动作。
Ticks 使用 with 或 close；不要依赖对象离开作用域后立即被回收。

CLI 接收 Ctrl-C 后会等待根清理。console exporter 本身也会被清理，因此不能只依靠
最后一条 dispose 日志是否出现来判断资源是否已经释放。
