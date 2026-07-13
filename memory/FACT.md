# Long-Term Workspace Facts

### 开发工作流

- **构建在 Docker 内执行**，不在宿主机安装依赖

---

## Operating rules

- All development, build, and verification work must be done inside Docker only.
- Do not install packages on the host or otherwise pollute the host environment.
- If Docker is unavailable or broken, stop development work and fix the Docker setup first.
- After code modifications, run Docker-only verification before reporting completion.
- Prefer local, reversible actions. Ask before destructive, shared-state, or hard-to-reverse operations.

## Project direction

- Building local Ubuntu developer tools: serial debugging, log viewing.
- Target: commercial-grade desktop software quality.

## Engineering principles

- 添加新功能不能影响旧功能。
- 修复问题不要引入新问题，不要修改无关代码。
- 第一性原理：从主流实现方法上找答案。
- 高性能是硬要求：日志多、打印快时不能出问题。
- 稳定性优先于花哨功能：响应检测宁可简单可靠，不要复杂易错。

## 串口调试工具设计原则（2026-07-10 整理）

### 设备端协议本质
- 设备发两类数据：**周期日志**（`783587 I model ...`）和 **AT 响应**（`OK,0%\r\n` / `OK%\r\n` / `ERROR,...%\r\n`）
- 两类数据都以 `\r\n` 或 `\n` 结尾——**只有完整行才有意义**
- AT 响应一定带 `\r\n` 终止符，格式是 `OK,args%\r\n` 或 `OK%\r\n`

### 串口后端接收原则（核心）
- **只发射完整行**：缓冲区攒到 `\r\n` 才算一行，没终止符的数据留在 buf 里等下一次
- **禁止读超时 flush 半行**：100ms 超时就把残行发出去 = 数据碎片化的根因，导致 `OK,0%\r\n` 被拆成 `OK,0` + `%` + `\r\n` 三段
- 如果担心无终止符的短帧卡住，用一个**更长的超时**（比如 500ms~1s）或日志显示"未完整行"标记，但不要把碎片当完整行发

### 显示原则
- **单窗口显示**：AT 发送/响应和周期日志放同一个 Text 控件，按时间顺序排列，用颜色区分（发送=蓝、响应=绿、日志=灰）
- **禁止过滤/截断设备数据**：设备发什么就显示什么，`OK,0%` 不能截成 `OK,0`
- **响应检测按内容**：完整行到达后检查是否 `OK`/`ERROR`/`+` 开头，匹配则配对 pending_cmd，否则当日志

### 反模式（禁止）
- ❌ 读超时 flush 残行 → 碎片化
- ❌ 用 search 在行内找 `OK,` 然后拆分 → 截断响应
- ❌ 双窗口分栏 → 发送和响应不在同一时间线上，难以对应
- ❌ 改 `_append` 共享方法加缓存/过滤 → 影响所有显示路径
