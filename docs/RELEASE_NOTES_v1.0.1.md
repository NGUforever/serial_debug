# serial_debug v1.0.1 Release Notes

- **版本号**: v1.0.1
- **发布日期**: 2026-08-18
- **Tag**: `v1.0.1`（commit `b423122`）
- **上一版本**: v1.0.0（TestDev 复审拒收）

## 概述

v1.0.0 经 Agent-Senior-TestDev 复审判定不通过（覆盖率 15%、跨平台 import 崩、F821 undefined-name ×4、719 处裸 `except Exception: pass`）。v1.0.1 针对拒收清单修复 **3 个 Blocker + 5 个 Major**，达到商用发布基线。

## 主要变更

### Blocker 修复（3 项）

1. **跨平台 import 崩溃** — `core/serial_backend.py:13` 模块级 `import fcntl` 改为 `try/except ImportError` 兜底，Windows/macOS 首次 import 不再崩溃。
2. **F821 undefined-name 漏修** — `log_viewer_window.py:245` `time.time()` 误用（应为 `_time.time()`），ruff check 全 src 复跑后修复。
3. **coverage omit 掩盖低覆盖率** — `pyproject.toml` 中 `qt_at_cmd_window` / `qt_monitor_window` / `qt_log_viewer_window` 三个核心 UI 文件（共 6576 行）从 coverage omit 名单移除，改用 pytest-qt + 集成测试覆盖。

### Major 修复（5 项）

1. 裸 `except Exception: pass` 719 处清理 — 关键 IO 边界补 `log_writer.write` 留故障现场，仅资源释放保留 pass 且加注释。
2. 正则安全护栏 — 用户输入正则走 `_is_dangerous_regex` 嵌套量词静态检查 + 行截断 8000 字符 + 墙钟超时（filter 800ms / search 5s），避免灾难性回溯冻 UI。
3. 行缓冲纪律 — pyserial reader 线程不直接调 Qt API，跨线程一律走 `Signal(...)`，PyInstaller 构建兼容。
4. 配置 schema 演进 — 新增字段必进 `DEFAULTS`，`save()` 只回写已登记字段防脏数据，向后兼容。
5. 去技术化命名 — 用户可见名统一为"串口调试助手"/"日志查看器"/"数据监视窗口"，不出现开发期字样。

## 已知限制

- **Windows 10/11 未实测**：静态跨平台审查已过（grep 平台特定 import / pathlib / 字体回退），但真机串口枚举、CH343 驱动绑定尚未跑过。详见 `docs/RELEASE_CHECKLIST_v1.0.1.md`。
- **macOS 14/15 未实测**：同上，Gatekeeper 签名 / 串口权限 / M 系列原生运行未跑过。
- **glibc 基线**：Ubuntu 22.04（glibc 2.35）为最低支持版本，更早版本需用户自行评估。

## 安装方式

### 方式 1：Linux 单文件可执行（推荐）

```bash
tar xzf serial_debug-linux-x86_64.tar.gz
cd serial_debug && ./run.sh
# 或直接 ./serial_debug/serial_debug
```

install_linux.sh 内置 glibc < 2.35 检测 + 自动 apt 缺失 Qt 共享库。

### 方式 2：pip 安装（源码运行）

```bash
pip install -r requirements.txt
python -m serial_debug
```

### 方式 3：源码模式（不走 PyInstaller，适合 exe 被公司安全机制拦截的环境）

```bash
python scripts/setup.py install_source
# 自动建 uv venv + 装依赖 + 创建桌面快捷方式
```

## 卸载

```bash
python scripts/setup.py uninstall         # 卸载 PyInstaller 安装
python scripts/setup.py uninstall_source  # 卸载源码模式
```

## 验收

- Agent-Senior-TestDev 复审通过（覆盖率 ≥ 60%，603 测试全过，无 Blocker）
- Linux verify smoke test 9 项全过（主窗口 + AT 面板 + 日志查看器 + 数据监视 + 解析器配置 + 格式校验 + 多语言日志 + 校验阈值 + 图例可见性）
- Linux Docker build（uv:python3.11-bookworm-slim 镜像）+ 本机 install 通过

## 下一步

v1.1.0-alpha 已发布（tkinter 兼容路径移除、解析器抽到 core/），本版本 v1.0.1 为 v1.0.x 维护分支最后一版。后续主线转入 v1.1.0 正版收尾。
