# 商用对标 — 功能借鉴与优化清单

> 整理时间：2026-08-15
> 适用项目：serial_debug（串口调试 + 日志查看 + 数据曲线监视 + AT 命令）
> 对标来源：Serial Studio、klogg、Foxglove Studio、PlotJuggler、CoolTerm、YAT、Docklight、RealTerm、Datplot、Telemetry Viewer、lnav、glogg

## 一、对标软件矩阵

| 软件 | 领域 | 关键借鉴点 |
|---|---|---|
| **Serial Studio** | 串口/设备可视化仪表盘 | 15+ 组件、JS/Lua 帧解析器、TCP/gRPC/MCP API、CSV 导出、SQLite 会话回放、XMODEM/YMODEM/ZMODEM、AI 助手、260k fps 解析管线 |
| **klogg** | 大文件日志查看 | 10GB+ 文件直接 mmap、布尔组合搜索 (AND/OR/NOT)、SIMD 加速正则、多编码自动检测、可配置高亮器集、文件变化监视 |
| **Foxglove Studio** | 机器人/ROS 可视化 | 布局云端同步、布局 JSON 备份、3D 场景、Plot 面板、Tab 多布局、MCP bridge、数据源历史 |
| **PlotJuggler** | 流式数据曲线 | 多曲线叠加/分离/XY 模式、CSV/ROS bag 回放、Lua 转换、统计面板 (min/max/mean/std)、缩放平移、PNG/SVG 导出 |
| **CoolTerm** | 串口调试 | 端口自动检测、连接向导、十六进制/文本切换、时间戳 |
| **YAT (Yet Another Terminal)** | 串口+AT 调试 | 命令序列、计时器循环发送、变量替换、十六进制发送 |
| **Docklight** | 串口协议仿真 | 脚本自动化、协议序列、ACK/NAK 等待、接收超时 |
| **RealTerm** | 工程串口 | 任意波特率、二进制流监视、端口锁定、信号线状态 |
| **Datplot / Telemetry Viewer** | 实时曲线 | 流式滚动、时间窗、双轴、阈值线 |

---

## 二、按功能模块的借鉴清单与优先级

> 标注：[已有] = 项目已实现；[部分] = 有但需增强；[缺] = 未实现；[P0/P1/P2] = 优先级

### 模块 A：串口核心 (core/serial_backend.py)

| 功能 | 现状 | 借鉴来源 | 优先级 | 建议实施 |
|---|---|---|---|---|
| 端口热插拔检测 | [部分] | CoolTerm 实时刷新 | P1 | 后台 QThread 监听 udev/pyserial 事件，UI 自动更新下拉 |
| 自定义波特率 + 任意整数 | [缺] | RealTerm | P2 | 在波特率下拉加"Custom…"项弹输入框 |
| 流控配置 (RTS/CTS/XON/XOFF) | 待确认 | 通用 | P1 | 配置面板加 FlowControl 选项 |
| 信号线状态显示 (RTS/CTS/DCD/DTR/RI) | [缺] | RealTerm/Docklight | P1 | 状态栏 LED 灯组，1s 刷新 `serial.COMM.get_cts()` 等 |
| 串口独占锁定 | [缺] | RealTerm | P2 | 打开时 `fcntl.flock` + 友好提示 |
| 发送队列 + 重试 | [缺] | YAT | P1 | TX 命令进入队列，失败重试 3 次 |
| 断线自动重连 | [缺] | Serial Studio | P1 | 可配置重连间隔 + 最大次数，回退策略 |

### 模块 B：AT 命令窗口 (ui/at_cmd_window.py, qt_at_cmd_window.py)

| 功能 | 现状 | 借鉴来源 | 优先级 | 建议实施 |
|---|---|---|---|---|
| AT 命令历史 (↑↓ 翻历史) | 待确认 | YAT | P0 | 状态保存最近 100 条，按 ↑↓ 切换 |
| 命令序列脚本 | [缺] | Docklight | P1 | 编辑器写 `sleep 0.5; AT+RST; wait "OK"; AT+CWMODE=1` 等序列化命令 |
| 循环发送 + 计时器 | [缺] | YAT | P1 | 周期循环发某条命令，可配置次数/间隔 |
| 变量替换 `${var}` | [缺] | YAT/Docklight | P1 | 从 JSON/环境变量取值替换 |
| 响应等待 + 超时 | [缺] | Docklight | P1 | 发送后等待 `OK`/`ERROR` 或 N 秒超时 |
| 命令模板库 | [部分] | YAT | P0 | 内置 ESP/4G/蓝牙模组常用 AT 命令集，按厂商分组 |
| 响应分类着色 (OK/ERROR/+xx) | [部分] | Foxglove 主题 | P0 | 正则 `^(OK[%,] \|ERROR\|\+\w+)` 分流到不同颜色 |
| 命令耗时显示 | [缺] | Docklight | P2 | 每条 AT 命令显示从发送到响应的 ms 数 |
| 批量执行 + 进度 | [缺] | YAT | P1 | 选中多条命令批量执行，进度条 |

### 模块 C：日志查看器 (ui/log_viewer_window.py, qt_log_viewer_window.py)

| 功能 | 现状 | 借鉴来源 | 优先级 | 建议实施 |
|---|---|---|---|---|
| 大文件流式加载 (mmap) | 待确认 | klogg | P0 | 文件不全部读入内存，按视口范围 seek+read |
| 多文件 Tab 标签 | 待确认 | klogg | P1 | 多日志文件并行打开 |
| 文件变化监视 (tail -f) | [部分] | klogg/glogg | P0 | inotify 监听 + 自动滚动到底 |
| 布尔组合搜索 (AND/OR/NOT) | [缺] | klogg | P1 | 搜索框支持 `error AND (timeout OR disconnect) NOT debug` |
| 高亮器集可切换 | 待确认 | klogg | P1 | 预置 INFO/WARN/ERROR/TIMESTAMP/HEX 等多个高亮规则集 |
| 搜索结果独立面板 | 待确认 | klogg/glogg | P1 | 搜索结果在新面板列出，点击跳转 |
| 编码自动检测 | 待确认 | klogg (uchardet) | P2 | UTF-8/UTF-16/GBK/CP1251 |
| 大文件跳行号 | 待确认 | klogg | P1 | 输入行号跳转，行号索引预建 |
| 时间戳范围过滤 | 待确认 | lnav | P1 | 时间轴滑动选择段范围 |
| 关键字收藏夹 | [缺] | klogg 预置正则 | P2 | 常用搜索词保存 |
| 文件夹书签 | [缺] | klogg | P2 | 收藏常用日志路径 |
| Scratchpad 笔记 | [缺] | klogg | P2 | 选中文本拖到侧栏做记录 |

### 模块 D：数据曲线监视 (ui/monitor_window.py, qt_monitor_window.py)

| 功能 | 现状 | 借鉴来源 | 优先级 | 建议实施 |
|---|---|---|---|---|
| 多曲线叠加/分离切换 | 待确认 | PlotJuggler | P0 | 每条曲线 checkbox 控制 visible + "分离显示"按钮 |
| XY 模式 | [缺] | PlotJuggler | P1 | 两条数据做 XY 散点图（如位置-速度相图） |
| 缩放/平移 | 待确认 | PlotJuggler | P0 | 鼠标滚轮缩放、拖动平移、双击复位 |
| 双 Y 轴 | [缺] | Datplot | P1 | 左轴电压、右轴电流等异单位数据共显 |
| 阈值线 | [缺] | Telemetry Viewer | P1 | 画水平/垂直警戒线 + 触发告警变色 |
| 统计面板 (min/max/mean/std) | [缺] | PlotJuggler | P0 | 当前视窗内每条曲线的统计值 |
| 流式滚动 + 时间窗 | 待确认 | Telemetry Viewer | P0 | 最近 N 秒数据 + 暂停按钮 |
| 数据游标 (双游标差值) | [缺] | PlotJuggler | P1 | 两条游标线，显示 Δt 和 Δy |
| CSV 导出 (当前可视范围) | 待确认 | Serial Studio | P0 | 不只全量，还能"导出选中段" |
| PNG/SVG 截图导出 | [缺] | PlotJuggler | P1 | 一键导出当前曲线图 |
| 帧解析器 (CSV/JSON/二进制) | [部分] | Serial Studio | P0 | 内置 CSV/JSON/COBS/SLIP 解析模板 |
| 数据转换 (EMA/缩放/标定) | [缺] | Serial Studio | P1 | 每条曲线可挂 JS/Lua 表达式 |
| 多通道子图布局 | [部分] | PlotJuggler | P1 | 网格布局可拖拽重排 |
| 历史回放 | [缺] | Serial Studio/PlotJuggler | P1 | 时间轴滑块拖到任意位置回放 |

### 模块 E：会话与持久化

| 功能 | 现状 | 借鉴来源 | 优先级 | 建议实施 |
|---|---|---|---|---|
| 会话录制 (帧+原始字节) | [缺] | Serial Studio SQLite | P1 | 录制到 SQLite，含时间戳/方向/原始字节 |
| 会话回放 | [缺] | Serial Studio | P1 | 从 SQLite 重放到 UI |
| 会话标签/备注 | [缺] | Serial Studio | P2 | 给录制段打 tag |
| 布局云端同步 | [缺] | Foxglove | P2 | 可选 gitee 私有库同步布局 JSON |
| 布局导入导出 JSON | 待确认 | Foxglove | P0 | 一键导出/导入整套 UI 配置 |
| 最近文件/最近连接 | 待确认 | 通用 | P1 | 启动页显示最近 10 个连接配置 |

### 模块 F：UI/主题/UX

| 功能 | 现状 | 借鉴来源 | 优先级 | 建议实施 |
|---|---|---|---|---|
| Dark/Light 主题切换 | [部分] | Foxglove | P0 | 跟随系统 + 手动切换 |
| 主题色定制 (强调色) | [缺] | VSCode | P2 | 设置里改 accent color |
| 字体可配置 + 等宽 | [部分] | klogg | P1 | 代码区固定等宽、UI 区可调 |
| 工具栏可拖拽/隐藏 | [缺] | 通用 | P2 | View 菜单控制工具栏可见性 |
| 状态栏信息 (fps/buffer/rate) | [缺] | Serial Studio | P1 | 实时显示 RX/TX 字节数、丢包率、帧率 |
| 全局快捷键 | [部分] | klogg 可配置 | P1 | 在 Preferences 里列出所有快捷键，可重映射 |
| 多语言 (i18n) | [缺] | 通用 | P2 | Qt linguist / gettext |
| 托盘最小化 | [缺] | 通用 | P2 | 关闭按钮最小化到托盘 |

### 模块 G：自动化与扩展

| 功能 | 现状 | 借鉴来源 | 优先级 | 建议实施 |
|---|---|---|---|---|
| TCP/HTTP API 远程控制 | [缺] | Serial Studio (port 7777) | P1 | 开 WebSocket 或 HTTP POST 控制连接/发送/录制 |
| 插件机制 (用户脚本) | [缺] | Serial Studio JS/Lua | P1 | 加载用户 .py/.lua 脚本作为帧解析或自定义按钮 |
| MCP bridge | [缺] | Foxglove/Serial Studio | P2 | 让 AI Agent 直接控制工具 |
| 命令行参数启动 | 待确认 | 通用 | P1 | `serial_debug --connect /dev/ttyUSB0 115200` |

---

## 三、推荐实施顺序（按价值/工作量比）

### 第一波（核心体验，1-2 周）
1. **A: 串口信号线状态显示 + 端口热插拔检测** — 高价值低成本
2. **C: 大文件 mmap 流式 + 布尔组合搜索** — 日志查看器核心体验
3. **D: 缩放平移 + 统计面板 + 当前段 CSV 导出** — 曲线监视刚需
4. **B: AT 命令历史 + 响应分类着色** — AT 调试刚需
5. **E: 布局 JSON 导入导出** — 用户配置可迁移

### 第二波（差异化，2-4 周）
6. **D: 帧解析器模板库 + 数据转换表达式** — 对标 Serial Studio
7. **B: 命令序列脚本 + 响应等待** — 对标 Docklight
8. **C: 时间戳范围过滤 + 文件变化监视** — 对标 klogg/lnav
9. **D: 双 Y 轴 + 阈值线 + 游标差值** — 对标 PlotJuggler
10. **E: 会话录制到 SQLite + 回放** — 对标 Serial Studio

### 第三波（高级扩展，4+ 周）
11. **G: 插件机制 + 用户脚本** — 对标 Serial Studio Painter
12. **G: TCP/HTTP API + MCP bridge** — 对标 Serial Studio/Foxglove
13. **F: 状态栏实时指标 + 全局快捷键可配置** — 对标 klogg
14. **E: 云端布局同步** — 对标 Foxglove（可选 gitee 私库）

---

## 四、技术实施建议

### 性能基线（对标 Serial Studio 的 CI benchmark gate）
- 解析管线目标：≥ 50,000 frames/s（Python tkinter 受限，可放宽到 10k）
- 大日志文件目标：≥ 1 GB 可流畅打开（mmap 必须）
- 曲线目标：≥ 100 万点流畅滚动（matplotlib 用 `set_xlim` 局部渲染，或换 pyqtgraph）

### 数据结构建议
- **帧缓冲**：环形数组 `collections.deque(maxlen=N)` 而非 list，避免内存爆炸
- **时间戳**：统一用 `time.monotonic()` + epoch 双轨，相对时间避免时钟回拨
- **持久化**：SQLite 表 `frames(id, ts, dir, raw_bytes, parsed_json)`，索引 ts
- **配置**：JSON schema 化，便于版本迁移

### UI 一致性
- 所有窗口共用 `qt_theme.py` 的 VSCode Dark+ 配色
- 数据呈现组件统一用 `option_add` 配 Listbox/Treeview 颜色
- 错误状态用 `QStatusBar.showMessage(temporary)` 而非弹窗

### 测试要求（AGENT.md 已规定）
- 单元测试覆盖：解析器、串口封装、CSV 导出
- 集成测试在 Docker 内跑
- 提交前 Agent-Senior-TestDev 复审

---

## 五、未抓到原文档的软件补充说明

### PlotJuggler（raw README 404，凭知识整理）
- 主要仓库：`faaxx86/PlotJuggler`，新版可能在 `faaxx86/plotjuggler3` 或 `faaxx86/PlotJuggler3`，但 raw 路径都 404
- 核心功能：流式数据绘图、CSV/ROS bag/mcap 回放、多曲线叠加/分离、XY 模式、双 Y 轴、Lua 转换、统计面板、PNG/SVG 导出、嵌入式 plot 拖拽、自定义插件
- 借鉴重点：**游标差值** + **统计面板** + **流式时间窗**

### Foxglove Studio（README 内容少，凭知识整理）
- 主要仓库：`foxglove/studio`，但 raw 404；可能改名为 `foxglove/foxglove`
- 核心功能：3D 场景、Plot 面板、Tab 多布局、布局 JSON 备份、ROS bag/mcap 回放、MCP bridge、布局云端同步、自定义 panel（TypeScript）
- 借鉴重点：**布局 JSON 备份/导入导出** + **Tab 多布局** + **数据源历史**

### YAT (Yet Another Terminal)
- 关键功能：命令序列、计时器循环、变量替换、命令模板库、十六进制发送、AT 命令分组
- 借鉴重点：**命令序列脚本语法**

### Docklight
- 关键功能：协议序列、ACK/NAK 等待、接收超时、自动化脚本、可视化序列图
- 借鉴重点：**响应等待 + 超时**

### CoolTerm
- 关键功能：端口自动检测、连接向导、十六进制/文本切换、时间戳、收藏夹
- 借鉴重点：**端口热插拔检测 + 收藏夹**

---

## 六、给 Create Tool Sets 的执行指令

1. **先读**：`Agent-Members/create_tools_set/AGENT.md` + `SOUL.md` + `memory/FACT.md` + 本文件
2. **现状摸查**：跑 `git log --oneline -10` 看最近提交，跑 `wc -l src/serial_debug/ui/*.py` 看代码规模，重点看 `qt_monitor_window.py` (174K)、`qt_log_viewer_window.py` (85K) 这两个核心
3. **按本文件第一波 5 项优先实施**，每项完成后：
   - 跑测试（Docker 内）
   - 写一条 `memory/JOURNAL.jsonl` 条目（工具名/语言/状态）
   - 提交一个独立 commit，便于 Senior-TestDev 复审
4. **超过 50 行核心逻辑改动前**：按 AGENT.md 规定询问用户
5. **不要碰**：AGENT.md / SOUL.md / 已通过复审的稳定代码

参考时间预算：第一波 5 项，每项 2-3 小时，合计 10-15 小时（含测试）。
