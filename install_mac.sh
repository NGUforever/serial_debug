#!/bin/bash
# macOS 安装脚本：将 serial_debug.app 安装到 /Applications
#
# 用法：
#   bash install_mac.sh          # 安装
#   bash install_mac.sh --uninstall   # 卸载
#
# 安装后：
#   - 应用程序：/Applications/serial_debug.app（可在 Spotlight / Launchpad 启动）
#   - 命令行：serial_debug（~/.local/bin 加入 PATH）
#
# 需要先运行 build_mac.sh 生成 dist/serial_debug.app

set -e

PROJECT_DIR=$(cd "$(dirname "$0")" && pwd)
cd "$PROJECT_DIR"

ACTION="install"
if [ "$1" = "--uninstall" ] || [ "$1" = "-u" ]; then
    ACTION="uninstall"
fi

APP_PATH="/Applications/serial_debug.app"
BIN_DIR="$HOME/.local/bin"

if [ "$ACTION" = "uninstall" ]; then
    echo "==> 卸载 serial_debug..."
    rm -rf "$APP_PATH"
    rm -f "$BIN_DIR/serial_debug"
    echo "==> 卸载完成"
    exit 0
fi

# 检查构建产物
if [ ! -d "dist/serial_debug.app" ]; then
    echo "错误：dist/serial_debug.app 不存在，请先运行 bash build_mac.sh"
    exit 1
fi

echo "==> 安装 serial_debug.app 到 /Applications ..."
rm -rf "$APP_PATH"
cp -R "dist/serial_debug.app" "$APP_PATH"
echo "  应用路径: $APP_PATH"

# 1. 创建命令行启动脚本
echo "==> 注册命令 serial_debug 到 PATH..."
mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/serial_debug" << EOF
#!/bin/bash
open -a "$APP_PATH" --args "\$@"
EOF
chmod +x "$BIN_DIR/serial_debug"
echo "  命令路径: $BIN_DIR/serial_debug"

echo ""
echo "==> 安装完成！"
echo ""
echo "使用方式："
echo "  1. Spotlight：搜索 'serial_debug' 或 '串口调试助手'"
echo "  2. Launchpad：在第一页或搜索"
echo "  3. 命令行：serial_debug（需 ~/.local/bin 在 PATH 中）"
echo "  4. 直接双击：/Applications/serial_debug.app"
echo ""
echo "PATH 检查（~/.local/bin 应在 PATH 中）："
echo "  echo \$PATH | grep -o '\.local/bin' && echo 'PATH OK' || echo '请将 export PATH=\$HOME/.local/bin:\$PATH 加入 ~/.zshrc'"
