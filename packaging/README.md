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

## v1.0.0 三平台 build 实测状态

> 更新时间：2026-08-15

### Linux — ✅ 已实测

- 实测环境：Ubuntu 7.0.0-29-generic host + uv venv (cpython 3.11.15) + PyInstaller 6.22.0
- 依赖：PySide6 6.11.1 / pyqtgraph 0.14.0 / numpy / PyOpenGL 3.1.10 / pyserial 3.5
- 构建：`pyinstaller --clean --noconfirm packaging/serial_debug.spec` 成功
- 产物：`dist/serial_debug/serial_debug` (6.8MB ELF) + `dist/serial_debug-linux-x86_64.tar.gz` (89MB)
- 体积：onedir 237MB
- 启动验证：`QT_QPA_PLATFORM=offscreen SERIAL_DEBUG_QT_MAIN=1 ./dist/serial_debug/serial_debug` 进入 Qt 事件循环（timeout 5s 退出码 124，无 stderr 报错，说明 Qt 主窗口构造完成且 import 链路全通）
- 已知 host 限制：tkinter fallback 入口需 `LD_LIBRARY_PATH` 含 uv python 的 libtcl9.0.so / libtk9.0.so，量产发行包由 PyInstaller 自动 bundle 这些 .so，最终用户不需要

### Windows — ⚠️ 待用户实测

**理由**：本任务执行环境为 Linux 主机。Wine 下跑 PySide6 + PyInstaller 不可靠（PySide6 Qt 平台插件依赖 Windows 原生 DLL，Wine 模拟常缺 Qt6Core.dll / Qt6Gui.dll / VCRuntime140.dll），且 Inno Setup 的 iscc.exe 也不能在 Wine 下稳定运行。选项 A（Wine 模拟）和选项 B（Docker mingw 交叉编译 PyInstaller）都不稳妥 —— PyInstaller 官方文档明确不支持 cross-compile。

**待用户在真实 Windows 10/11 机器上跑以下 checklist**：

1. 安装 Python 3.11+ （勾选 "Add to PATH"）
2. 安装 VC++ Redistributable 2015-2022 x64（[下载](https://aka.ms/vs/17/release/vc_redist.x64.exe)）
3. clone 仓库：`git clone https://gitee.com/ericslzwang/serial_debug.git`
4. 装依赖：`pip install -r requirements.txt pyinstaller`
5. 构建：双击 `packaging\build_win.bat`，或 `pyinstaller --clean --noconfirm packaging\serial_debug.spec`
6. 验证启动：双击 `dist\serial_debug\serial_debug.exe`，确认主窗口、AT 面板、日志查看器、数据监视都能打开
7. （可选）Inno Setup 打包：装 [Inno Setup 6](https://jrsoftware.org/isdl.php)，右键 `packaging\serial_debug-installer.iss` → Compile，产出 `dist\serial_debug-setup.exe`
8. 把验证结果（截图 / 错误日志）回传到 issue tracker

### macOS — ⚠️ 待用户实测

**理由**：同 Windows。Linux 上无法生成有效的 `.app` bundle 和 `.dmg`。

**待用户在真实 macOS 13+ (Intel 或 Apple Silicon) 机器上跑以下 checklist**：

1. 装 Xcode Command Line Tools：`xcode-select --install`
2. 装 Python 3.11+ （推荐用 Homebrew：`brew install python@3.11`）
3. clone + 装依赖（同 Windows step 3-4）
4. 构建：`bash packaging/build_mac.sh`
5. 验证启动：双击 `dist/serial_debug.app`，确认主窗口、AT 面板、日志查看器、数据监视都能打开
6. Gatekeeper：若提示"来自未知开发者"，跑 `bash packaging/install_mac.sh`，会自动 `xattr -dr com.apple.quarantine`
7. （可选）打 dmg：`hdiutil create -volname serial_debug -srcfolder dist/serial_debug.app -ov -format UDZO dist/serial_debug-macos.dmg`
8. Apple Silicon 用户额外验证：PyOpenGL 在 arm64 下偶有问题，若曲线窗口报 OpenGL 错，尝试 `pip install --force-reinstall PyOpenGL`
9. 把验证结果回传到 issue tracker

### Inno Setup — ⚠️ 待用户在 Windows 上跑

`packaging/serial_debug-installer.iss` 是 Inno Setup 脚本，**不能在 Linux 上交叉编译**（iscc.exe 是 Windows 原生程序，Wine 下不稳定）。选项 B（Docker mingw + iscc 交叉）需要 Windows SDK 和 Inno Setup 全套环境，不划算。

**待用户在 Windows 上装 Inno Setup 6 后**：右键 `serial_debug-installer.iss` → Compile，产出 `dist/serial_debug-setup.exe`。
