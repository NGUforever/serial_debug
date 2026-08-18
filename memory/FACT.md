# FACT — serial_debug 项目

## 项目定位

`serial_debug` 是要**公开发布**的桌面级嵌入式软件开发工具集合，目标用户为嵌入式/电机调试工程师。当前主线是串口调试工具，后续会扩展为工具集合。**后续可能以插件形式嵌入某 IDE**，因此代码结构需保持模块化、入口可独立、不绑死单一 host。

代码库：`/home/ninebot/tools/Personal-AI-Zone/Agent-Members/create_tools_set/src/serial_debug/`
安装产物：`~/.local/share/serial_debug/`，命令 `serial_debug` / `serial_debug_log` 入 `~/.local/bin/`。

## 团队编制

| 角色 | 职责 |
|---|---|
| **Agent-Head** | 领导 + 联合评审 |
| **用户（ninebot）** | 需求方 + 联合评审 |
| **Agent-Senior-TestDev** | 测试 |
| **PD_manager Agent** | PC 环境管理、打包功能安全性 + 适用性协作 |
| **本 Agent** | 嵌入式软件开发工具集合开发（当前主线：serial_debug） |

评审 = 用户 + Agent-Head。跨 Agent 接口变更需走 PD_manager + Senior-TestDev 协作通道。

## 技术栈基线（不动）

- **GUI**：PySide6（唯一入口，tkinter 兼容路径已于 v1.1.0-alpha 移除）
- **曲线**：pyqtgraph + numpy（LTTB 降采样）
- **打包**：PyInstaller（`packaging/build_linux.sh` + `install_linux.sh`），跨平台目标
- **配置**：`~/.config/serial_debug/config.json`，原子写 + DEFAULTS schema 演进
- **依赖最小化**：用户机器可能没装编译工具链，只依赖运行时二进制

## 关键不变量

1. **PyInstaller 兼容**：跨线程用 `Signal(...)` 而非 `Q_ARG(list)`——后者 PyInstaller 构建会崩。子进程入口用环境变量（`SERIAL_DEBUG_QT_PILOT=1` 等）而非 `python -m`，因 bootloader 不识别 `-m`。
2. **行缓冲纪律**：pyserial 仅在 `\r\n` 终止时 emit 完整行；reader 线程不直接调 Qt API，跨线程一律走 Qt 信号。
3. **正则安全**：用户输入正则走 `_is_dangerous_regex` 嵌套量词静态检查 + 行截断（8000 字符）+ 墙钟超时（filter 800ms / search 5s），避免灾难性回溯冻 UI。
4. **配置 schema 演进**：新增字段必进 `DEFAULTS`，`save()` 只回写已登记字段防脏数据。旧 config 缺字段走默认值，向后兼容。
5. **目录记忆**：不同 file dialog（打开日志 / 导入配置 / 导出配置 / 导出 CSV）各自独立 config 键记上次目录，避免互相覆盖。
6. **去技术化命名**：用户可见名（窗口标题、菜单、关于、错误弹窗）一律用正式名"串口调试助手"/"日志查看器"/"数据监视窗口"，不出现 "试点"/"测试"/"Stage X"/"PySide6 + pyqtgraph" 等开发期字样。源码内部注释不在约束内。

## 跨进程单例约束

`serial_debug_log` 通过符号链接指向同一二进制 + `setApplicationName('serial_debug_log')` + desktop `StartupWMClass=serial_debug_log`，三重匹配让 GNOME dock 不误识别为"串口调试助手"。改启动方式时三处必须同步。

## 风险线

- 用户机器可能 glibc < 2.35（Ubuntu 22.04 基线），install_linux.sh 内置检测 + 自动 apt 缺失 Qt 共享库。
- IDE 插件形态未定，对外 API 边界尚未冻结——目前不要为"未来插件化"提前抽象，按当前桌面形态做，未来再切。
- 自定义解析器 `exec` 用户代码——本地工具不设沙箱，但 dialog 显著提示"仅可信来源粘贴"。
