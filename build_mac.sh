#!/bin/bash
# macOS .app 构建脚本
#
# 用法：
#   bash build_mac.sh
#
# 依赖（自动安装）：
#   pip install pyinstaller pyserial
#
# 输出：
#   dist/serial_debug.app
#
# 可选：签名 + 公证（需 Apple Developer 账号）
#   codesign --deep --force --sign "Developer ID Application: XXX" dist/serial_debug.app
#   xcrun notarytool submit dist/serial_debug.app.zip --apple-id YOUR_ID --password YOUR_PW

set -e

PROJECT_DIR=$(cd "$(dirname "$0")" && pwd)
cd "$PROJECT_DIR"

# 安装依赖
pip install --quiet pyinstaller pyserial

# PyInstaller 构建
pyinstaller --clean --noconfirm serial_debug.spec

# 打包 .dmg（可选，需要 create-dmg）
if command -v create-dmg >/dev/null 2>&1; then
    echo "==> 打包 DMG..."
    create-dmg \
        --volname "串口调试助手" \
        --window-size 600 400 \
        --icon "serial_debug.app" 150 100 \
        --app-drop-link 450 100 \
        "dist/serial_debug.dmg" \
        "dist/serial_debug.app" || echo "DMG 打包跳过（可能有警告）"
fi

echo ""
echo "==> 完成！输出:"
ls -lh dist/serial_debug.app 2>/dev/null
ls -lh dist/serial_debug.dmg 2>/dev/null
echo ""
echo "使用方式："
echo "  双击 serial_debug.app 运行"
echo "  首次打开可能需在 系统偏好设置 > 安全性与隐私 中允许"
