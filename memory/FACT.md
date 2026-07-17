# Long-Term Workspace Facts

### 开发工作流

- **构建在 Docker 内执行**，不在宿主机安装依赖
- **uv 虚拟环境**：`/home/ninebot/tools/common_py_env`（用 `source bin/activate` 激活，不是 pyenv）

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

## UI 风格基线（2026-07-13 第一版商业化改造）

### 选定方向
- VSCode/PyCharm 深色专业风
- 配色取自 VSCode Dark+（深色）+ 浅色版本
- 字体：DejaVu Sans Mono 10（日志区），DejaVu Sans 9（控件）
- 不用 Courier——Linux 上 DejaVu Sans Mono 渲染更清晰

### UI 设计原则
- 扁平按钮（relief=flat, borderwidth=0, padding=(10,4)）+ 三态（normal/hover/active）
- Primary 按钮用 accent 蓝色 #0e639c 区分主操作（发送/连接）与次要操作
- 顶栏分组用 ttk.Separator 垂直分隔：连接组 | 工具组 | 主题组 | 右侧状态点
- 状态用 Canvas 画 12×12 圆点（灰=未连接/绿=已连接/黄=等待响应/红=错误），比纯文字更直观
- 日志区加 spacing2/spacing3=2 + padx/pady=6/8 透气
- TTK Style 用 clam theme + 自定义 TButton/Primary.TButton/TEntry focus 边框
- tk.Canvas 需要手动跟随主题（ttk 控件自动跟随 root style，Canvas 不跟随）

### 反模式（禁止）
- ❌ 默认 tkinter 主题（极简、土气、不像商业软件）
- ❌ 用 Courier 字体（Linux 渲染差）
- ❌ 纯文字状态（不直观）
- ❌ 所有按钮同色（无主次区分）

### tkinter UI 天花板
- ttk + clam theme + 自定义 style map 能做到 VSCode 风格的"专业感"
- 做不到 Qt/QML 那种细腻效果（圆角、阴影、动画）
- 后续在此基础上逐步丰富，不换框架

## 串口调试工具设计原则（2026-07-10 整理）

### 设备端协议本质
- 设备发两类数据：**周期日志**（`783587 I model ...`）和 **AT 响应**（`OK,0%\r\n` / `OK%\r\n` / `ERROR,...%\r\n`）
- 两类数据都以 `\r\n` 或 `\n` 结尾——**只有完整行才有意义**
- AT 响应一定带 `\r\n` 终止符，格式是 `OK,args%\r\n` 或 `OK%\r\n`

### 串口后端接收原则（核心）
- **只发射完整行**：缓冲区攒到 `\r\n` 才算一行，没终止符的数据留在 buf 里等下一次
- **禁止读超时 flush 半行**：100ms 超时就把残行发出去 = 数据碎片化的根因
- 如果担心无终止符的短帧卡住，用一个**更长的超时**或日志显示"未完整行"标记，但不要把碎片当完整行发

### 显示原则
- **单窗口显示**：AT 发送/响应和周期日志放同一 Text 控件，按时间顺序排列，用颜色区分
- **禁止过滤/截断设备数据**：设备发什么就显示什么
- **响应检测按内容**：完整行到达后检查是否 `OK`/`ERROR`/`+` 开头，匹配则配对 pending_cmd，否则当日志
- **RESP_RE** = `^(OK[%,]|ERROR|\+)`，匹配 `OK,` `OK%` `OK,0%` `ERROR,...` `+CMD`，不匹配日志行

### 反模式（禁止）
- ❌ 读超时 flush 残行 → 碎片化
- ❌ 用 search 在行内找 `OK,` 然后拆分 → 截断响应
- ❌ 双窗口分栏 → 发送和响应不在同一时间线上，难以对应
- ❌ 改 `_append` 共享方法加缓存/过滤 → 影响所有显示路径

## USB 串口芯片 / Linux 驱动知识（2026-07-13）

### CH343（VID:PID=1a86:55d5，USB Quad_Serial 4 端口）
- **Linux cdc_acm 通用驱动不初始化 latency timer**——drivers/usb/class/cdc-acm.c 的 acm_port_activate() 不设 ASYNC_LOW_LATENCY、不调 usb_set_interface、无 latency timer 写入
- **用户态 TIOCSSERIAL + ASYNC_LOW_LATENCY 对 cdc_acm 无效**——驱动不读这个 flag
- **ch341.ko 不支持 CH343**（id_table 只含 5523/7522/7523，不含 55d5）
- **必须装 WCH 官方 out-of-tree 驱动** github.com/WCHSoftGroup/ch343ser_linux
  - 模块名 ch343.ko，driver name usb_ch343
  - 覆盖 55d2/55d3/55d4/55d5/55d7/55d8/55db/55dd/55da/55de/55e7/55df/55e9/55ea/55e8/55eb/55ec
  - V2.1（2026.02）关键改动："set low_latency flag in tty activate routine"
  - 装完节点变 /dev/ttyCH343USB0~3
- **pyserial list_ports() 不识别 ttyCH343USBx**——list_ports_linux 不扫非标准命名的 tty，Combobox 下拉看不到，需要在 list_ports() 里兜底扫 /dev/ttyCH343USB* /dev/ttyUSB*

### 安装步骤
```bash
git clone https://github.com/WCHSoftGroup/ch343ser_linux
cd ch343ser_linux/driver && make
sudo make install
sudo modprobe ch343
cd .. && sudo cp udev/99-ch34x.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger
# 重新插拔 USB，节点出现
```

### Short packet 延迟本质
- USB 转串口芯片有 latency timer（CH343 默认 ~16ms），靠 vendor 控制传输设置
- cdc_acm 通用驱动不设这个寄存器，CH343 用上电默认值
- 表现：OK 响应（8 字节 short packet）卡在 USB bulk IN FIFO，直到周期日志大批量数据涌入才被一起推出
- Windows CH343SER.SYS vendor 驱动初始化 latency timer，所以同事的 Windows 工具工作正常

## CH343 驱动抢占问题（2026-07-16 修复）

### 问题
- 重启后 `/dev/ttyCH343USB0-3` 消失，只剩 `/dev/ttyACM0-3`
- 原因：cdc_acm 在 USB 枚举时先匹配 CDC ACM 接口（class 0x02）抢占接口 2-7，ch343 的 `in*` alias 没机会执行
- CH343 Quad_Serial 每个端口用 2 个 interface（control + data），共 8 个接口；ch343 只抢到接口 0,1（端口 0），其余被 cdc_acm 抢

### 持久化修复（已部署到 /etc/udev/rules.d/99-ch343-override.rules）
- 不 blacklist cdc_acm（不影响其他 ACM 设备）
- udev 规则：当 cdc_acm 匹配到 1a86:55d5 时，立即 `echo usb_ch343 > /sys$DEVPATH/driver_override` 改绑
- 规则路径：`/etc/udev/rules.d/99-ch343-override.rules`
- 脚本：`/home/ninebot/projects/serial_debug/scripts/fix_ch343_driver.sh`

### 验证方式
- `ls /dev/ttyCH343USB*` 应有 4 个
- `ls /sys/bus/usb/drivers/usb_ch343/ | grep :` 应有 8 个接口
- `cat /sys/bus/usb/devices/*/driver_override` 查看 override 设置
