#!/bin/bash
# Linux 安装脚本：将 serial_debug 安装到 ~/.local/bin 并注册桌面菜单
#
# 用法：
#   bash install_linux.sh            # 安装
#   bash install_linux.sh --uninstall  # 卸载
#   bash install_linux.sh --desktop    # 安装 + 创建桌面快捷方式
#
# 安装后：
#   - 可执行文件：~/.local/bin/serial_debug（直接命令行输入 serial_debug 启动）
#   - 应用菜单：自动出现"串口调试助手"图标
#   - 应用菜单：自动出现"日志查看器"图标（MimeType=text/plain;text/x-log;...）
#   - 文件管理器：右键日志文件 → 打开方式 → 可选"日志查看器"
#   - 桌面快捷方式：可选（--desktop 参数）
#
# 需要先运行 build_linux.sh 生成 dist/serial_debug/ 目录

set -e

# spec 在 packaging/ 下，仓库根在上一层
PROJECT_DIR=$(cd "$(dirname "$0")/.." && pwd)
cd "$PROJECT_DIR"

ACTION="install"
MAKE_DESKTOP=0
if [ "$1" = "--uninstall" ] || [ "$1" = "-u" ]; then
    ACTION="uninstall"
elif [ "$1" = "--desktop" ]; then
    MAKE_DESKTOP=1
fi

# 安装目录
INSTALL_DIR="$HOME/.local/share/serial_debug"
BIN_DIR="$HOME/.local/bin"
APP_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/hicolor"

if [ "$ACTION" = "uninstall" ]; then
    echo "==> 卸载 serial_debug..."
    rm -rf "$INSTALL_DIR"
    rm -f "$BIN_DIR/serial_debug"
    rm -f "$APP_DIR/serial_debug.desktop" "$APP_DIR/serial_debug_log.desktop"
    # 删除两类图标：调试助手 + 日志查看器
    rm -rf "$ICON_DIR/256x256/apps/serial_debug.png" \
           "$ICON_DIR/48x48/apps/serial_debug.png" \
           "$ICON_DIR/scalable/apps/serial_debug.svg" \
           "$ICON_DIR/256x256/apps/serial_debug_log.png" \
           "$ICON_DIR/48x48/apps/serial_debug_log.png" \
           "$ICON_DIR/128x128/apps/serial_debug_log.png" \
           "$ICON_DIR/scalable/apps/serial_debug_log.svg"
    update-desktop-database "$APP_DIR" 2>/dev/null || true
    update-mime-database "$HOME/.local/share/mime" 2>/dev/null || true
    echo "==> 卸载完成"
    exit 0
fi

# 检查构建产物
if [ ! -d "dist/serial_debug" ]; then
    echo "错误：dist/serial_debug/ 不存在，请先运行 bash packaging/build_linux.sh"
    exit 1
fi

# 检测 PySide6/Qt 运行时必需的共享库（缺则自动 sudo apt install）
_check_qt_libs() {
    # libxcb-* / libGL / libEGL / libfontconfig / libdbus
    # 缺任何一个，PySide6 启动崩 "Could not load Qt platform plugin xcb"
    local pkgs=(
        libxcb-xinerama0 libxcb-cursor0 libxcb-icccm4 libxcb-image0
        libxcb-keysyms1 libxcb-randr0 libxcb-render-util0 libxcb-shape0
        libxcb-xkb1 libxkbcommon-x11-0 libgl1 libegl1 libfontconfig1
        libdbus-1-3
    )
    local missing=()
    for p in "${pkgs[@]}"; do
        if ! dpkg -s "$p" >/dev/null 2>&1; then
            missing+=("$p")
        fi
    done
    if [ ${#missing[@]} -gt 0 ]; then
        echo "==> 检测到缺 PySide6/Qt 运行时库：${missing[*]}"
        echo "    不装会崩 \"Could not load Qt platform plugin xcb\""
        # root 用户直接 apt（容器内 sudo 可能不存在），普通用户用 sudo
        if [ "$(id -u)" = "0" ]; then
            APT="apt"
        else
            APT="sudo apt"
        fi
        echo "==> 自动安装：$APT install ${missing[*]}"
        $APT update -qq
        $APT install -y "${missing[@]}" || {
            echo "    自动安装失败，请手动执行："
            echo "    sudo apt install ${missing[*]}"
            echo "    然后重跑 install_linux.sh"
            exit 1
        }
    fi
}

# 检测 glibc >= 2.35（Ubuntu 22.04 LTS 是构建基线）
_check_glibc() {
    local ver
    ver=$(ldd --version 2>/dev/null | head -n1 | awk '{print $NF}')
    if [ -z "$ver" ]; then
        echo "    警告：无法检测 glibc 版本"
        return
    fi
    local major minor
    major=$(echo "$ver" | cut -d. -f1)
    minor=$(echo "$ver" | cut -d. -f2)
    if [ "$major" -lt 2 ] || { [ "$major" -eq 2 ] && [ "$minor" -lt 35 ]; }; then
        echo "==> 警告：glibc $ver < 2.35"
        echo "    Ubuntu 22.04 构建的二进制可能跑不起来"
        echo "    建议：升级系统到 Ubuntu 22.04+ 或用 docker ubuntu:22.04 构建"
    fi
}

echo "==> 检测运行时依赖..."
_check_qt_libs
_check_glibc

echo "==> 安装 serial_debug 到 $INSTALL_DIR ..."
mkdir -p "$INSTALL_DIR"
cp -r dist/serial_debug/* "$INSTALL_DIR/"

# 1. 命令行启动脚本到 ~/.local/bin
echo "==> 注册命令 serial_debug / serial_debug_log 到 PATH..."
mkdir -p "$BIN_DIR"
# 串口调试助手：直接 exec binary
cat > "$BIN_DIR/serial_debug" << EOF
#!/bin/bash
exec "$INSTALL_DIR/serial_debug" "\$@"
EOF
chmod +x "$BIN_DIR/serial_debug"
# 日志查看器：通过符号链接 serial_debug_log 启动，让 /proc/PID/comm =
# serial_debug_log，配合 setApplicationName('serial_debug_log') 和 desktop
# file 的 StartupWMClass=serial_debug_log，GNOME dock 三重匹配都对得上，
# 不会再误识别为"串口调试助手"。
if [ -f "$INSTALL_DIR/serial_debug" ]; then
    ln -sf serial_debug "$INSTALL_DIR/serial_debug_log"
fi
cat > "$BIN_DIR/serial_debug_log" << EOF
#!/bin/bash
exec "$INSTALL_DIR/serial_debug_log" --view "\$@"
EOF
chmod +x "$BIN_DIR/serial_debug_log"
echo "  命令路径: $BIN_DIR/serial_debug / $BIN_DIR/serial_debug_log"
echo "  现在 'serial_debug' 和 'serial_debug_log' 命令可用"

# 2. 安装图标：串口调试 + 日志查看器
echo "==> 安装图标..."
mkdir -p "$ICON_DIR/256x256/apps" "$ICON_DIR/128x128/apps" \
         "$ICON_DIR/48x48/apps" "$ICON_DIR/scalable/apps"
_ASSETS="$PROJECT_DIR/src/serial_debug/resources/assets"
# 串口调试助手图标（icon_debug 系列）
cp "$_ASSETS/icon_debug-256.png" "$ICON_DIR/256x256/apps/serial_debug.png"
cp "$_ASSETS/icon_debug-48.png"  "$ICON_DIR/48x48/apps/serial_debug.png"
cp "$_ASSETS/icon_debug.svg"     "$ICON_DIR/scalable/apps/serial_debug.svg"
# 日志查看器图标（icon_view 系列）→ 文件名 serial_debug_log
cp "$_ASSETS/icon_view-256.png"  "$ICON_DIR/256x256/apps/serial_debug_log.png"
cp "$_ASSETS/icon_view-128.png"  "$ICON_DIR/128x128/apps/serial_debug_log.png"
cp "$_ASSETS/icon_view-48.png"   "$ICON_DIR/48x48/apps/serial_debug_log.png"
cp "$_ASSETS/icon_view.svg"      "$ICON_DIR/scalable/apps/serial_debug_log.svg"

# 3. 注册 .desktop 文件
echo "==> 注册到应用菜单..."
mkdir -p "$APP_DIR"

# 3.1 串口调试助手
# StartupNotify=false：tkinter 不实现 _NET_STARTUP_ID，true 会让 GNOME
# 超时判定启动失败。StartupWMClass=serial_debug 与 app.setApplicationName
# 对齐，让 GNOME dock 正确匹配本 desktop file。
cat > "$APP_DIR/serial_debug.desktop" << EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=串口调试助手
GenericName=Serial Debug Tool
Comment=串口调试工具：CH343 实时收发 / AT 命令管理 / 日志存储
Comment[zh_CN]=串口调试工具：CH343 实时收发 / AT 命令管理 / 日志存储
Exec=$BIN_DIR/serial_debug
Icon=serial_debug
Terminal=false
Categories=Development;Utility;
Keywords=serial;uart;AT;debug;ch343;
StartupNotify=false
StartupWMClass=serial_debug
EOF

# 3.2 日志查看器（支持文件管理器右键"打开方式"）
# Exec 指向独立 wrapper 脚本 serial_debug_log（exec -a 改 argv[0]），
# 让 GNOME 识别为与串口助手不同的应用，避免前置串口助手窗口。
# StartupWMClass=serial_debug_log 与 app.setApplicationName('serial_debug_log')
# 对齐，让 GNOME dock 匹配到本 desktop file 而非主程序。
cat > "$APP_DIR/serial_debug_log.desktop" << EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=日志查看器
GenericName=Log File Viewer
Comment=日志文件查看工具：多文件 Tab + 筛选面板 + 全局搜索
Comment[zh_CN]=日志文件查看工具：多文件 Tab + 筛选面板 + 全局搜索
Exec=$BIN_DIR/serial_debug_log %F
Icon=serial_debug_log
Terminal=false
Categories=Utility;TextTools;
Keywords=log;viewer;text;grep;filter;
StartupNotify=false
StartupWMClass=serial_debug_log
MimeType=text/plain;text/x-log;text/x-readme;application/octet-stream;text/*;
EOF

update-desktop-database "$APP_DIR" 2>/dev/null || true
# MimeType 改动后刷新 mime-database（部分文件管理器如 Nautilus 需要）
update-mime-database "$HOME/.local/share/mime" 2>/dev/null || true

# 4. 可选：桌面快捷方式
if [ $MAKE_DESKTOP -eq 1 ]; then
    echo "==> 创建桌面快捷方式..."
    cp "$APP_DIR/serial_debug.desktop" "$HOME/Desktop/serial_debug.desktop"
    chmod +x "$HOME/Desktop/serial_debug.desktop"
fi

echo ""
echo "==> 安装完成！"
echo ""
echo "使用方式："
echo "  1. 命令行：serial_debug"
echo "  2. 应用菜单：搜索'串口调试助手'或'日志查看器'"
echo "  3. 文件管理器：右键 .txt/.log 文件 → 打开方式 → 选'日志查看器'"
echo "  4. 桌面快捷方式（如安装时加了 --desktop）"
echo ""
echo "PATH 检查（~/.local/bin 应在 PATH 中）："
echo "  echo \$PATH | grep -o '\.local/bin' && echo 'PATH OK' || echo '请将 export PATH=\$HOME/.local/bin:\$PATH 加入 ~/.bashrc'"
