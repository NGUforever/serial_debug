# 跨平台构建 / 安装

## 一键命令（推荐）

任意平台 clone 仓库后执行：

```
python scripts/setup.py all       # build + install
python scripts/setup.py build    # 仅构建 dist/ 二进制
python scripts/setup.py install  # 仅安装到本机
python scripts/setup.py uninstall
python scripts/setup.py check     # 仅自检
```

`setup.py` 自动检测平台、自检依赖、自动修复（pip 镜像源 fallback /
sudo apt install 系统库 / VC++ Redist 下载），修不了的问题给明确指令。

## Linux

### 开发者构建

```
bash packaging/build_linux.sh
```

输出 `dist/serial_debug/` + `dist/serial_debug-linux-x86_64.tar.gz`。

### 终端用户安装

```
bash packaging/install_linux.sh          # 安装
bash packaging/install_linux.sh --desktop  # 加桌面快捷方式
bash packaging/install_linux.sh --uninstall
```

安装到 `~/.local/share/serial_debug/`，注册 `serial_debug` /
`serial_debug_log` 命令到 `~/.local/bin/`，桌面菜单出现"串口调试助手"
和"日志查看器"图标。

### Docker 构建（保证 glibc 兼容）

Ubuntu 22.04 LTS 是构建基线，二进制要求目标机 glibc >= 2.35：

```
docker run --rm -v $(pwd):/work -w /work ubuntu:22.04 \
  bash -c "apt-get update && apt-get install -y python3 python3-pip python3-tk tk-dev binutils && bash packaging/build_linux.sh"
```

### 常见依赖问题

#### 缺 PySide6/Qt 运行时库

报错：`Could not load Qt platform plugin "xcb"`

修复：

```
sudo apt install libxcb-xinerama0 libxcb-cursor0 libxcb-icccm4 \
  libxcb-image0 libxcb-keysyms1 libxcb-randr0 libxcb-render-util0 \
  libxcb-shape0 libxcb-xkb1 libxkbcommon-x11-0 libgl1 libegl1 \
  libfontconfig1 libdbus-1-3
```

`install_linux.sh` 已自动检测 + sudo apt install。

#### glibc 版本不兼容

报错：`./serial_debug: /lib/x86_64-linux-gnu/libc.so.6: version
GLIBC_2.35 not found`

原因：在 Ubuntu 22.04 (glibc 2.35) 上构建的二进制，在更老系统
（CentOS 7 / Ubuntu 18.04 / Debian 10）上跑不起来。

修复：用 docker ubuntu:22.04 重新构建，或升级目标系统。

#### pip 装包失败

`build_linux.sh` 已用 `scripts/pip_install_with_fallback.py`，
依次重试 4 个镜像源：官方 → 清华 → 阿里 → 豆瓣。

诊断网络：

```
curl -I https://pypi.org/simple/
curl -I https://pypi.tuna.tsinghua.edu.cn/simple/
```

## Windows

### 开发者构建

```
packaging\build_win.bat
```

输出 `dist\serial_debug\` 目录。`build_win.bat` 自动检测 VC++ Redist
（注册表 `HKLM\SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\x64\Installed`）。

### 终端用户安装

```
packaging\install_win.bat              :: 安装
packaging\install_win.bat --uninstall  :: 卸载
packaging\install_win.bat --desktop    :: 加桌面快捷方式
```

安装到 `%LOCALAPPDATA%\serial_debug\`，注册 `serial_debug` 命令到
`%LOCALAPPDATA%\bin\`（自动加 PATH），创建开始菜单"串口调试助手"快捷方式。

### 常见依赖问题

#### 缺 VC++ Redistributable 2015-2022 x64

报错：`缺 VCRuntime140.dll` / `缺 MSVCP140.dll`

`install_win.bat` 已自动检测，缺失时下载 `https://aka.ms/vs/17/release/vc_redist.x64.exe`
静默安装。也可手动下载安装。

### 便携 zip 分发

直接打包 `dist\serial_debug\` 整个目录为 zip 分发，用户解压后双击
`serial_debug.exe` 即可运行，无需 install。

## macOS

### 开发者构建

```
bash packaging/build_mac.sh
```

输出 `dist/serial_debug.app`。脚本检测 Xcode CLT（`xcode-select -p`）。

### 终端用户安装

```
bash packaging/install_mac.sh          :: 安装
bash packaging/install_mac.sh --uninstall
```

安装到 `/Applications/serial_debug.app`，注册 `serial_debug` 命令到
`~/.local/bin/`。脚本自动解除 Gatekeeper quarantine 标记
（`xattr -dr com.apple.quarantine`）。

### 常见依赖问题

#### Gatekeeper "无法打开，因为来自未知开发者"

`install_mac.sh` 已自动 `xattr -dr com.apple.quarantine`。若仍弹窗：

```
sudo xattr -rd com.apple.quarantine /Applications/serial_debug.app
```

或在"系统偏好设置 > 安全性与隐私"点"仍要打开"。

#### 缺 Xcode Command Line Tools

```
xcode-select --install
```

## 跨平台依赖自检

```
python scripts/check_env.py
```

## 文件清单

| 文件 | 作用 |
|---|---|
| `scripts/setup.py` | 跨平台一键入口 |
| `scripts/check_env.py` | 依赖自检 + 自动修复 |
| `scripts/pip_install_with_fallback.py` | pip 多镜像源 fallback |
| `packaging/build_linux.sh` | Linux PyInstaller 构建 |
| `packaging/build_mac.sh` | Mac PyInstaller 构建 |
| `packaging/build_win.bat` | Windows PyInstaller 构建 |
| `packaging/install_linux.sh` | Linux 安装到 ~/.local/share |
| `packaging/install_mac.sh` | Mac 安装到 /Applications |
| `packaging/install_win.bat` | Windows 安装到 %LOCALAPPDATA% |
| `packaging/serial_debug.spec` | PyInstaller 跨平台 spec |
| `requirements.txt` | Python 依赖列表 |

## 三平台 build 实测状态

> 更新时间：2026-09-21（v1.1.0）

### CI 自动构建（GitHub Actions）

三平台 CI 已接入 `.github/workflows/release.yml`，打 `v*` tag 自动触发，产出 draft release：

| 平台 | runner | 安装包 | 免安装版 |
|---|---|---|---|
| Linux | ubuntu-22.04 | `install_linux.sh`（仓库内置） | `serial_debug-linux-x86_64.tar.gz` + `serial_debug-x86_64.AppImage` |
| Windows | windows-latest | `serial_debug-windows-x64-setup.exe`（Inno Setup） | `serial_debug-windows-x64-portable.zip` |
| macOS | macos-latest (arm64) | `serial_debug-macos-arm64.dmg` | `serial_debug-macos-arm64-portable.zip` |

依赖锁定到本地实测快照（2026-09-21）：pyinstaller 6.22.2 / pyserial 3.5 /
PySide6 6.11.1 / pyqtgraph 0.14.0 / numpy 2.5.2 / PyOpenGL 3.1.10。
CI 用 Python 3.12。Linux 固定 ubuntu-22.04（glibc 2.35 基线），
不跟随 ubuntu-latest 升级以免破坏"目标机 glibc >= 2.35"承诺。
macOS 先只产 arm64（macos-latest 为 Apple Silicon），不做 universal2——
PyInstaller 在只有 arm64 解释器的 runner 上产 universal2 会失败或实际只产 arm64。
Intel 用户方案：后续单独加 macos-13 x86_64 job，或本地 `build_mac.sh` 自行构建。

### Linux — ✅ 已实测（v1.1.0）

- 实测环境：Docker ubuntu:22.04 + Python + PyInstaller（CI 同基线）
- 构建：`pyinstaller --clean --noconfirm packaging/serial_debug.spec` 成功
- 产物：`dist/serial_debug/serial_debug` (ELF x86-64, stripped) +
  `dist/serial_debug-linux-x86_64.tar.gz` (72MB) + 便携 `run.sh` 启动脚本
- 启动验证：`QT_QPA_PLATFORM=offscreen SERIAL_DEBUG_QT_MAIN=1 ./dist/serial_debug/serial_debug`
  进入 Qt 事件循环（timeout 5s 退出码 124，无 stderr 报错，Qt 主窗口构造完成且 import 链路全通）
- v1.1.0 起：tkinter 兼容路径已全量移除，`LD_LIBRARY_PATH` 不再需要 libtcl/libtk；
  PyInstaller 不再 bundle tcl/tk 运行时，发行包体积下降

### Windows — ⚠️ 待 CI 首次触发验证

CI 已接入（`choco install innosetup` + `iscc` 编译产 setup.exe + 便携 zip），待打 tag
首次触发后在真实 Windows 10/11 验证：安装/卸载/快捷方式/便携 zip 启动。
PyInstaller 不支持 Linux 交叉编译 Win，Wine 下 PySide6/Inno Setup 不可靠，
故不在本机验证，依赖 CI runner 原生环境。

CI 产物下载后验证清单：
1. 安装包：双击 `serial_debug-windows-x64-setup.exe` → 开始菜单出现"串口调试助手"
   和"日志查看器"；控制面板可卸载
2. 便携版：解压 `serial_debug-windows-x64-portable.zip` → 双击 `serial_debug.exe` 启动
3. 缺 VC++ Redistributable 2015-2022 x64 时（`VCRuntime140.dll` 缺失），
   安装包已自动检测并下载 `https://aka.ms/vs/17/release/vc_redist.x64.exe` 静默安装

### macOS — ⚠️ 待 CI 首次触发验证

CI 已接入（`hdiutil` 产 dmg + `ditto` 产 portable zip），待打 tag 首次触发后在真实
macOS 13+ (Apple Silicon) 验证：dmg 拖拽安装 / portable 启动 / Gatekeeper。
PyInstaller 不支持 Linux 交叉编译 mac，故不在本机验证。

CI 产物下载后验证清单：
1. 安装包：双击 `serial_debug-macos-arm64.dmg` → 拖拽 serial_debug.app 到 Applications
2. Gatekeeper：首次右键打开绕"来自未知开发者"（或跑 `install_mac.sh` 自动
   `xattr -dr com.apple.quarantine`）
3. 便携版：解压 `serial_debug-macos-arm64-portable.zip` → 双击 `.app`
4. Apple Silicon 额外验证：若曲线窗口报 OpenGL 错，`pip install --force-reinstall PyOpenGL`

### Inno Setup — ⚠️ 待 CI 首次触发验证

`packaging/serial_debug-installer.iss` 已对齐 v1.1.0（AppVersion / AppId GUID /
OutputBaseFilename=serial_debug-windows-x64-setup / VersionInfoVersion）。
CI 里 `choco install innosetup` + `iscc` 编译，产 `dist/serial_debug-windows-x64-setup.exe`。
不在本机验证（iscc.exe 是 Windows 原生程序，Wine 下不稳定）。
