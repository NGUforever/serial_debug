# serial_debug v1.0.1 Win/Mac 实测 Checklist

- **版本**: v1.0.1
- **准备日期**: 2026-08-18
- **背景**: Linux 已通过 verify smoke test + Docker build + 本机 install。Windows/macOS 仅有静态跨平台审查，真机未实测。用户跑完本 checklist 后反馈结果给 Agent-Head 决策是否转正发布。

## 静态跨平台审查（已过，无需用户跑）

- [x] grep 平台特定 import（fcntl / termios / msvcrt / winreg）— 模块级 try/except 兜底已加
- [x] 路径处理统一用 pathlib
- [x] 字体/颜色/串口名跨平台
- [x] ruff check 全 src/ 通过
- [x] mypy 全 src/ 通过

---

## Windows 10/11 实测项

### 准备
- [ ] 下载 `serial_debug-win-x86_64.zip`（或源码 zip）
- [ ] 解压到任意目录（如 `C:\serial_debug\`）
- [ ] VC++ Redistributable 2015-2022 x64 已装（PySide6 运行时依赖，缺则首启崩 "VCRuntime140.dll"）
  - 下载：https://aka.ms/vs/17/release/vc_redist.x64.exe

### 启动与基础功能
- [ ] 双击 `serial_debug.exe` 能启动主窗口（不崩）
- [ ] 主窗口标题显示"串口调试助手"（不含开发期字样）
- [ ] 菜单栏可点击展开
- [ ] 关闭主窗口不残留进程

### 串口枚举
- [ ] 串口下拉框能列出本机所有 COM 口（含 USB-串口适配器）
- [ ] 拔插 USB-串口适配器，下拉框能动态刷新
- [ ] 能选中一个 COM 口并连接（不崩）

### AT 命令面板
- [ ] 切到"AT 命令"标签页能显示面板
- [ ] 输入 AT 命令点发送，能收到设备响应
- [ ] 响应历史滚动正常
- [ ] 正则筛选不冻 UI（输入 `^(OK|ERROR|\+)` 测试）

### 日志查看器
- [ ] 从主菜单或快捷方式打开"日志查看器"
- [ ] 能拖入 .log/.txt 文件加载（多文件 Tab）
- [ ] 筛选面板（正则、级别、关键字）能生效
- [ ] 全局搜索能命中并高亮
- [ ] 大文件（> 50MB）加载不崩

### 数据监视窗口
- [ ] 主窗口开"数据监视"能弹出曲线窗口
- [ ] 实时数据曲线滚动正常
- [ ] 图例 checkbox 可切换曲线可见性
- [ ] LTTB 降采样对长数据不卡

### CH343 连接（如硬件可用）
- [ ] 插入 CH343 USB-串口适配器，Windows 自动识别（或装官方驱动）
- [ ] 串口下拉框列出 CH343 对应 COM 口
- [ ] 能连接 CH343 + 收发数据正常
- [ ] 波特率切换 115200 / 921600 都能稳定收发

---

## macOS 14/15 实测项

### 准备
- [ ] 下载 `serial_debug-macos.tar.gz`（或源码 zip）
- [ ] 解压到 `~/Applications/` 或任意目录
- [ ] 首启 Gatekeeper 拦截 → 系统设置 → 隐私与安全性 → 允许打开
  - 后续如需签名分发，单独评估 Apple Developer ID 签名成本

### 启动与基础功能
- [ ] 双击 `serial_debug` 能启动主窗口（不崩）
- [ ] 主窗口标题显示"串口调试助手"
- [ ] dock 图标显示正常（不显示为"Python"或问号）
- [ ] 关闭主窗口不残留进程

### 串口枚举（macOS 串口名为 /dev/cu.* 与 /dev/tty.*）
- [ ] 串口下拉框列出 `/dev/cu.usbserial-*` 和 `/dev/cu.Bluetooth-Incoming-Port` 等
- [ ] 首次选串口需授权（macOS 串口权限弹窗）
- [ ] 能选中一个串口并连接

### AT 命令面板
- [ ] 同 Windows 项

### 日志查看器
- [ ] 同 Windows 项
- [ ] 文件管理器右键 .log/.txt → 打开方式 → 日志查看器（MimeType 注册生效）

### 数据监视窗口
- [ ] 同 Windows 项
- [ ] OpenGL 后端正常（Mac M 系列原生 Metal，Intel Iris OpenGL）

### CH343 连接（如硬件可用）
- [ ] 插入 CH343 USB-串口适配器，macOS 自动识别为 `/dev/cu.usbserial-*`
- [ ] 串口下拉框能列出
- [ ] 能连接 + 收发数据正常
- [ ] 如出现"driver_override 需手动"提示，按 Linux 的 cdc_acm 抢占策略手动设（Mac 类似路径在 macOS 上需评估）

---

## 反馈格式

跑完后填表回传：

| 平台 | 启动 | 串口枚举 | AT 命令 | 日志查看 | 数据监视 | CH343 | 结论 |
|------|------|---------|--------|---------|--------|------|------|
| Windows 10/11 | ✓/✗ | ✓/✗ | ✓/✗ | ✓/✗ | ✓/✗ | ✓/✗/N/A | 通过/失败原因 |
| macOS 14/15 | ✓/✗ | ✓/✗ | ✓/✗ | ✓/✗ | ✓/✗ | ✓/✗/N/A | 通过/失败原因 |

失败的项请附：
1. 操作系统版本（`winver` / `sw_vers`）
2. 报错日志（控制台输出 / 崩溃对话框截图）
3. 复现步骤

反馈给 Agent-Head 后决策是否转 v1.0.1 正式发布。
