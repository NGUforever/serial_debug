#!/bin/bash
# Linux 便携版构建脚本
#
# 用法：
#   bash build_linux.sh              # 仅 tar.gz 便携压缩包
#   bash build_linux.sh --appimage   # 同时尝试构建 AppImage（需可访问 GitHub）
#
# 依赖（自动安装）：
#   pip install pyinstaller pyserial
#   系统包：binutils tk-dev（Debian/Ubuntu: apt install）
#
# 输出：
#   dist/serial_debug-linux-x86_64.tar.gz   （便携压缩包，主推）
#   dist/serial_debug-x86_64.AppImage        （可选，--appimage 时）
#
# 建议：在 Docker ubuntu:22.04 中执行以保证 glibc 兼容性
#   docker run --rm -v $(pwd):/work -w /work ubuntu:22.04 bash build_linux.sh

set -e

# spec 在 packaging/ 下，仓库根在上一层
PROJECT_DIR=$(cd "$(dirname "$0")/.." && pwd)
cd "$PROJECT_DIR"

MAKE_APPIMAGE=0
if [ "$1" = "--appimage" ]; then
    MAKE_APPIMAGE=1
fi

# 安装 Python 依赖（多镜像源 fallback，避免单源失败就死）
echo "==> 安装 Python 依赖（pip 镜像源 fallback）..."
python3 scripts/pip_install_with_fallback.py -r requirements.txt pyinstaller

# PyInstaller 构建
echo "==> PyInstaller 构建中..."
pyinstaller --clean --noconfirm packaging/serial_debug.spec

# 添加启动脚本（解压后直接运行 run.sh 或 ./serial_debug）
cat > dist/serial_debug/run.sh << 'EOF'
#!/bin/bash
HERE="$(dirname "$(readlink -f "$0")")"
exec "${HERE}/serial_debug" "$@"
EOF
chmod +x dist/serial_debug/run.sh

# 打包 tar.gz 便携压缩包
echo "==> 打包 tar.gz..."
ARCH=$(uname -m)
TARBALL="dist/serial_debug-linux-${ARCH}.tar.gz"
tar -czf "$TARBALL" -C dist serial_debug

echo ""
echo "==> tar.gz 完成："
ls -lh "$TARBALL"
echo ""
echo "使用方式："
echo "  tar xzf serial_debug-linux-${ARCH}.tar.gz"
echo "  cd serial_debug && ./run.sh"
echo "  或直接 ./serial_debug/serial_debug"

# 可选：构建 AppImage
if [ $MAKE_APPIMAGE -eq 1 ]; then
    echo ""
    echo "==> 尝试构建 AppImage..."

    # 组装 AppDir
    rm -rf AppDir
    mkdir -p AppDir/usr/bin AppDir/usr/share/applications
    mkdir -p AppDir/usr/share/icons/hicolor/256x256/apps
    mkdir -p AppDir/usr/share/icons/hicolor/48x48/apps

    cp -r dist/serial_debug/* AppDir/usr/bin/
    cp desktop/serial_debug.desktop AppDir/usr/share/applications/
    cp src/serial_debug/resources/assets/icon_debug.png AppDir/usr/share/icons/hicolor/256x256/apps/serial_debug.png
    cp src/serial_debug/resources/assets/icon_debug-48.png AppDir/usr/share/icons/hicolor/48x48/apps/serial_debug.png

    cat > AppDir/AppRun << 'EOF'
#!/bin/bash
HERE="$(dirname "$(readlink -f "$0")")"
export PATH="${HERE}/usr/bin:${PATH}"
export LD_LIBRARY_PATH="${HERE}/usr/lib:${LD_LIBRARY_PATH}"
exec "${HERE}/usr/bin/serial_debug" "$@"
EOF
    chmod +x AppDir/AppRun

    # 下载 linuxdeploy
    if [ ! -f linuxdeploy-x86_64.AppImage ]; then
        echo "下载 linuxdeploy..."
        wget -q --timeout=30 \
            https://github.com/linuxdeploy/linuxdeploy/releases/download/continuous/linuxdeploy-x86_64.AppImage \
            -O linuxdeploy-x86_64.AppImage || {
                echo "警告：linuxdeploy 下载失败（网络受限）"
                echo "跳过 AppImage 构建，仅保留 tar.gz"
                rm -rf AppDir
                exit 0
            }
        chmod +x linuxdeploy-x86_64.AppImage
    fi

    ARCH=x86_64 ./linuxdeploy-x86_64.AppImage \
        --appdir AppDir \
        --desktop-file AppDir/usr/share/applications/serial_debug.desktop \
        --icon-file AppDir/usr/share/icons/hicolor/256x256/apps/serial_debug.png \
        --output appimage || {
            echo "警告：AppImage 构建失败，仅保留 tar.gz"
            rm -rf AppDir
            exit 0
        }

    echo "==> AppImage 完成："
    ls -lh *.AppImage 2>/dev/null && mv *.AppImage dist/
fi
