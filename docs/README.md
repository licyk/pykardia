# pykardia 使用文档

pykardia 把应用拆成插件。每个插件拥有配置和资源，可以提供服务，也可以依赖
其他插件的服务。框架负责依赖就绪后的启动，以及更新、卸载时的资源清理。

## 选择使用方式

| 方式 | 适合的场景 | 从哪里开始 |
|---|---|---|
| Python 库 | 在自己的程序中注册插件，直接调用服务 | [快速开始](getting-started.md)、[插件与服务](plugins.md) |
| 配置驱动 | 用 YAML/JSON 组织内置插件及动态库/WASM guest | [配置应用](configuration.md) |
| Guest | 将插件作为独立进程、动态库或 WASM 文件交付 | [Guest 开发](guests.md) |

建议先运行一个 Python 插件，再引入服务依赖、配置文件或热更新。

## 阅读路线

1. [安装与快速开始](getting-started.md)：安装、运行第一个插件、使用命令行。
2. [插件与服务](plugins.md)：配置类型、依赖声明、更新和确定性清理。
3. [配置应用](configuration.md)：条目、分组、隔离、include、表达式和写回。
4. [常用服务](resources.md)：计时器、文件监听、事件、控制台日志。
5. [热更新](hot-reload.md)：监听文件、手动更新模块以及处理失败。
6. [Guest 开发](guests.md)：SDK、三种导出、宿主加载和部署条件。

需要查询接口时使用 [API 参考](api.md)，遇到问题时查看 [故障排查](troubleshooting.md)。

## 运行环境

安装要求 Python 3.10+，测试矩阵覆盖 Python 3.10–3.15 与 Linux、Windows、macOS。
各组合的运行结果以 CI 为准。WASM guest 的构建工具链目前针对 Linux x86_64 / Python 3.11。
普通应用安装不需要 C 编译器或 WASI SDK；导出 guest 时的要求见对应指南。

这些指南中的 shell 命令，除特别说明外，都在仓库源码目录执行。
可运行示例保存在 [examples](../examples/)。
