# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 规范，版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### 新增

- **原始数据显示模式**：主程序底栏新增「原始模式」checkbox，开启后串口收到的行原样追加到日志区，不解析 LOG_TS、不识别响应、不过滤。专为开机阶段固件 printf 打印的纯文本日志设计——此时 RTOS 还未启动 ringbuffer+DMA 异步传输，日志无 13 位 ms 时间戳和类型前缀，解析模式会丢失可读性。新增 `core/raw_renderer.py`（`RawBuffer` 有界缓冲 + `format_raw_line` 行尾归一化 + `decode_chunk` utf-8 兜底解码），配置 schema 新增 `raw_display_enabled`。落盘 log_writer 在两种模式下都正常工作，开机日志可追溯。

## [1.1.0] — 2026-08-18

### 变更

- **移除 tkinter 兼容路径**：v1.0.x 双轨 `SERIAL_DEBUG_TK=1` 回退在 v1.1.0 全量废弃，仅保留 Qt (PySide6) 入口。删除 tkinter 主程序 / 数据监视窗口 / AT 命令面板 / 日志查看器 / AutoCombobox 共 4 个文件 ~6700 行；`app.py` 从 1769 行收缩到 137 行纯入口路由；`infra/themes.py` 剥离 `apply_theme` / `apply_menu_theme` 等 tkinter-only 函数；移除 `SERIAL_DEBUG_TK` 环境变量与所有 `import tkinter` / `from tkinter` 引用。
- **解析器模块抽出**：原 tkinter `ui/monitor_window.py` 的 motor / BMS / IMU Panel 解析器（`_parse_tagged_line` + `DEFAULT_VARS` + `_extract_log_ts`）抽到 `core/panel_vars.py` (470 行) + `core/log_parser.py` (37 行)，无 GUI 依赖，Qt 路径与新测试共用。`qt_monitor_window.py` 改从新模块 import，调用面零改动。
- **pyproject.toml**：version `1.0.0` → `1.1.0`；keywords 删除 `tkinter`、新增 `PySide6`。
- **README / docs/README**：删除 `tkinter` 依赖提及，更新为 PySide6 / pyqtgraph / numpy / PyOpenGL。

### 测试与覆盖率

- 测试用例：519 → 603（+84，新增 `test_app.py` 入口路由 7 + `test_qt_app.py` 路由/timestamp/drain/append_log 55 + `test_panel_vars.py` 解析器 34；删除 tkinter 旧测试 12）
- 整体覆盖率：35% → 65%（达 65-70% 目标区间下界）
- 目标文件覆盖率：`core/panel_vars.py` 97% / `core/log_parser.py` 90% / `infra/themes.py` 100% / `app.py` 51% / `qt_app.py` 0%→38%
- 静态分析：ruff P0 (E9/F) 清零，P2 (B007/E741) 7 项沿用 v1.0.1 延后策略

## [1.0.0] — 2026-08-15

首个稳定版本发布。经过 33 个 commit 的商业化收尾（feature/at-command-window-theme 分支），核心串口调试 + 日志查看 + 数据监视 + AT 命令 + Qt 迁移全部落地，三平台打包脚本就绪。

### 新增

- **Qt 主程序 Stage 4.3-4.5**：底栏正则筛选 + LogWriter 独立线程落盘 + 日志查看器子进程入口 + 默认 Qt 入口
- **Qt 主程序 Stage 4.1 / 4.2**：串口连接 + 日志显示 + 发送骨架；AT 命令面板 QTableWidget + 行内编辑 + 批量执行
- **Qt 监视窗口 Stage 1-3**：多 plot 子图 + 配置持久化 + 变量列表侧栏 + 统计对话框 + CSV 导出
- **Qt 日志查看器 Phase 1-5**：多 Tab + 行号 + 50k 批渲染 + 滚动加载 + 文件内 / 全局搜索 + 筛选面板 DockWidget + 实时刷新 + 几何持久化 + popout 单图
- **Qt 试点曲线窗口 + LTTB 降采样**：10w 点 → 2000 点 12.9ms，50 个尖峰全保留；统计/hover 仍用全量数据
- **Qt 性能基准达标**：100w 点 × 3 条曲线渲染 55ms（PyOpenGL + numpy 向量化 + PlotDataItem 复用 setData）
- **数据监视布局持久化**：曲线布局 / 图例 checkbox / LOG_TS 推导
- **数据监视导入/导出曲线图配置 + 拖拽宽高**
- **AT 命令面板流式布局**：窄屏自动换行
- **install_source 源码模式安装**：用 uv 加速（比 venv + pip 快 10-100 倍）
- **verify + ship 命令**：Docker offscreen smoke test 验证通过才覆盖稳定版
- **fix_ch340_driver.sh / fix_ch343_driver.sh 一键修复**：build + install + udev + rebind
- **跨平台安装 + 依赖自动解决**：pip 兼容 PEP 668（Ubuntu 24.04+ externally-managed-environment）

### 变更

- **Development Status**：从 Beta（4）升级到 Production/Stable（5）
- **默认入口迁到 Qt**：tkinter 版保留为 `SERIAL_DEBUG_TK=1` 回退；监视窗口解析器（`_parse_tagged_line` + `DEFAULT_VARS`）继续复用不重写
- **AGENT.md / SOUL.md 角色定义重写**：删除冗余 USER.md

### 修复

- 日志查看器左侧空白列（行号区从未绘制）
- 图例颜色方框与曲线颜色对应
- AT 面板 splitter 拖动跳变 + 底栏按钮被压缩文字截断
- 实时串口 feed 不生成曲线（`_iter_panel_vars` 提取不到 ts 直接 return）
- AT 命令导出 JSON 漏最后一行（编辑器未 commit）
- 悬停 HTML 不生效 + 曲线只标 max/min
- 监视窗口图例消失 + 曲线缺失 + 悬停不显示

### 已知限制

- Windows / macOS 实测打包尚未在真实环境验证（仅 Linux 完成 Docker 内 PyInstaller build 实测），见 [packaging/README.md](packaging/README.md)
- 单元测试覆盖仍偏薄（仅 smoke 层骨架），下一步扩充解析器 / 串口封装 / CSV 导出测试
- 静态分析 ruff/mypy P1/P2 warning 待后续迭代修复

### 商业化方向

v1.0.0 起，仓库采用 **核心开源（MIT）+ 商业增强版闭源** 双轨策略，闭源部分位于 `commercial/` 目录（v1.0.0 仅占位骨架）。详见 [commercial/COMMERCIAL.md](commercial/COMMERCIAL.md)。

## Roadmap

- 信号线状态显示 (RTS/CTS/DCD/DTR/RI)
- 端口热插拔检测
- 大文件 mmap 流式加载
- 布尔组合搜索 (AND/OR/NOT)
- 双 Y 轴 / 阈值线 / 游标差值
- 会话录制到 SQLite + 回放
- 插件机制（用户脚本）
- TCP/HTTP API + MCP bridge
