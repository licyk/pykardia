# pykardia

[![PyPI](https://img.shields.io/pypi/v/pykardia)](https://pypi.org/project/pykardia/)
[![Python](https://img.shields.io/pypi/pyversions/pykardia)](https://pypi.org/project/pykardia/)
[![CI](https://github.com/licyk/pykardia/actions/workflows/ci.yml/badge.svg)](https://github.com/licyk/pykardia/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-GPL--3.0--only-blue)](LICENSE)

pykardia 是一个 Python 插件框架，用来组织可动态加载、更新和卸载的功能。
插件可以提供服务、声明依赖，并把计时器、监听器和清理动作交给框架管理。
当服务被替换或撤销时，依赖它的插件会随之重新加载或等待。

- 用 Python 编写插件，通过类型注解声明配置和服务依赖。
- 使用 YAML/JSON 管理插件，支持配置监听和运行时修改写回。
- 为独立的插件组隔离服务，按模块变化执行热更新。
- 加载进程、动态库和 WASM guest，也可以将 Python guest 导出为这三种产物。

## 开始使用

需要 Python 3.10 或更高版本。
从 PyPI 安装：

```sh
python -m pip install pykardia
```

下载或克隆本仓库后，在仓库目录运行示例：

```sh
python examples/provider_consumer.py
```

也可以启动配置驱动的应用：

```sh
pykardia examples/pykardia.yml
```

它会加载示例 WASM 插件，并监听配置和插件文件。按 Ctrl-C 停止并清理资源。
`python -m pykardia` 与 `pykardia` 是等价入口；不传路径时读取 `pykardia.yml`。

## 使用文档

从 [文档首页](docs/README.md) 或 [快速开始](docs/getting-started.md) 阅读：

| 你想做什么 | 文档 |
|---|---|
| 编写插件、提供服务、管理资源 | [插件与服务](docs/plugins.md) |
| 使用 YAML/JSON、分组、隔离和配置写回 | [配置应用](docs/configuration.md) |
| 使用计时器、文件监听、事件和日志 | [常用服务](docs/resources.md) |
| 更新正在运行的模块 | [热更新](docs/hot-reload.md) |
| 编写、导出和加载独立插件 | [Guest 开发](docs/guests.md) |
| 查找常用类型和方法 | [API 参考](docs/api.md) |
| 处理启动、依赖和运行时问题 | [故障排查](docs/troubleshooting.md) |

许可证为 [GPL-3.0-only](LICENSE)，第三方声明见 [NOTICE](NOTICE)。guest SDK（`pykardia/guest/` 及其打包进 guest 的模块，文件头标有 SPDX）可任选 [MIT](pykardia/guest/LICENSE-MIT) 或 [Apache-2.0](pykardia/guest/LICENSE-APACHE)，guest 可以自行选择许可证。
