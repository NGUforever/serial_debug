# serial_debug

> 商用级串口调试助手 + 日志查看器 + 实时数据监视 — Linux / Windows / macOS 三平台桌面应用。
> 由 Ninebot 嵌入式团队维护。

[![Release](https://img.shields.io/badge/release-v1.1.0-blue)](CHANGELOG.md)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue)](pyproject.toml)
[![Platform](https://img.shields.io/badge/platform-Linux%20%7C%20Windows%20%7C%20macOS-lightgrey)](#系统要求)

## 功能一览

- **实时收发**：CH343 / CH340 / CDC-ACM 全支持，VMIN=1 VTIME=0 阻塞读模式与 minicom 一致；发送栏支持追加 CRLF、十六进制、命令历史
- **AT 命令管理**：表格化命令列表 + 行内编辑 + 批量周期执行 + JSON 导入导出；可独立 / 嵌入式 / 隐藏三态布局
- **日志存储**：独立写入线程消费队列，磁盘 I/O 不阻塞收发
- **日志查看器**：多文件 Tab + 行号 + 50k 批渲染 + 滚动加载更多 + Ctrl+F 文件内搜索 + Ctrl+Shift+F 全局搜索 + 筛选面板 DockWidget 嵌入/分离 + 实时刷新 + 视图状态持久化 + 时间戳转换
- **实时数据监视**：变量级 regex 提取 + 多 plot 子图 + 拖拽宽高 + 颜色/标签/统计持久化 + CSV 导出 + 横轴时间格式 + 100w 点 55ms 渲染（PySide6 + pyqtgraph + PyOpenGL）
- **VSCode Dark+ / Breakers Light 双主题**：跟随系统 + 手动切换
- **桌面集成**：Linux 文件管理器右键 `.txt/.log` → 打开方式 → 日志查看器
- **CH343 驱动一键修复**：`scripts/fix_ch343_driver.sh` 解决 cdc_acm 通用驱动下 short packet 延迟问题

## 截图

> 截图位（v1.0.0 发布后补充）

- 主窗口（串口连接 + 日志显示 + AT 面板）
- 日志查看器（多 Tab + 筛选 + 双向定位）
- 数据监视窗口（多 plot 子图 + 统计 + CSV 导出）
- 主题切换（VSCode Dark+ ↔ Breakers Light）

## 安装

### 预编译包（推荐）

从 [Gitee Release](https://gitee.com/ericslzwang/serial_debug/releases) 下载对应平台安装包：

| 平台 | 包格式 | 安装方式 |
|---|---|---|
| Linux | `serial_debug-linux-x86_64.tar.gz` | `bash install_linux.sh --desktop` |
| Windows | `serial_debug-setup.exe` | 双击 Inno Setup 安装 |
| macOS | `serial_debug-macos.dmg` | 拖入 Applications |

### 源码模式

```bash
git clone https://gitee.com/ericslzwang/serial_debug.git
cd serial_debug
pip install -e .
serial_debug
```

## 使用

```bash
# 直接运行
serial_debug

# 用日志查看器打开 .txt/.log
serial_debug --view path/to/log1.txt log2.log

# Qt 主程序（默认入口，PySide6 + pyqtgraph）
SERIAL_DEBUG_QT_MAIN=1 serial_debug

# 仅启动日志查看器
SERIAL_DEBUG_LOG_VIEWER=1 serial_debug
```

详见 [docs/README.md](docs/README.md)。

## 打包

```bash
# Linux（Ubuntu 22.04 / AppImage）
bash packaging/build_linux.sh [--appimage]

# Windows（PyInstaller + Inno Setup）
packaging\build_win.bat

# macOS（PyInstaller + dmg）
bash packaging/build_mac.sh
```

## 系统要求

- Python 3.9+
- pyserial >= 3.5
- PySide6 >= 6.5（Qt 主程序 + 数据监视 + 日志查看器）
- pyqtgraph / numpy / PyOpenGL（数据监视曲线渲染）
- Linux 桌面集成可选：`update-desktop-database` / `update-mime-database`

## 双轨商业化

本项目采用 **核心开源（MIT）+ 商业增强版闭源** 双轨策略：

- 开源核心（本仓库，MIT）：串口收发 + AT 命令 + 日志查看器 + 数据监视 + 双主题
- 商业增强版（闭源，`commercial/`）：License 校验、高级 AT 模板库、增强日志分析（布尔组合搜索 / SQLite 会话录制）、MCP bridge / TCP-HTTP API 远程控制、插件机制、云端布局同步

详见 [COMMERCIAL.md](commercial/COMMERCIAL.md) 与 [docs/COMMERCIAL_BENCHMARK.md](docs/COMMERCIAL_BENCHMARK.md)。

## 贡献

- 开发流程见 [AGENT.md](AGENT.md)
- 提交前 `python3 scripts/setup.py verify`（Docker offscreen smoke test）
- 验证通过 `python3 scripts/setup.py ship`（verify + build + install）

## License

[MIT](LICENSE) — Copyright (c) 2026 ericslzwang / Ninebot

商业增强版受 [commercial/COMMERCIAL.md](commercial/COMMERCIAL.md) 中另行声明的私有协议约束。
