# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec 文件 - 串口调试助手跨平台打包。

构建命令：
  pyinstaller serial_debug.spec            # 当前平台默认输出
  pyinstaller --clean serial_debug.spec    # 清理缓存重建

输出：
  Linux:   dist/serial_debug/        (onedir，后续用 build_linux.sh 转 AppImage)
  Windows: dist/serial_debug/       (onedir，可用 Inno Setup 打包成安装器)
  macOS:   dist/serial_debug.app     (app bundle)
"""
import sys
from PyInstaller.utils.hooks import collect_submodules

# pyserial 延迟导入 list_ports 平台子模块，必须显式收集
hiddenimports = collect_submodules('serial')

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('assets', 'assets'),           # 图标资源
        ('common_at.json', '.'),        # 预置 AT 命令
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=['matplotlib', 'numpy', 'pytest', 'IPython',
              'setuptools', 'pip', 'distutils'],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

# 平台条件：构建时 sys.platform 为当前构建平台
_platform = sys.platform

if _platform == 'darwin':
    # macOS: .app bundle
    exe = EXE(
        pyz, a.scripts, [],
        exclude_binaries=True,
        name='serial_debug',
        debug=False,
        strip=False,
        upx=False,
        console=False,
        icon='assets/icon.png',
        target_arch='universal2',
        codesign_identity=None,
        entitlements_file=None,
    )
    coll = COLLECT(
        exe, a.binaries, a.zipfiles, a.datas,
        name='serial_debug',
    )
    app = BUNDLE(
        coll,
        name='serial_debug.app',
        icon='assets/icon.png',
        bundle_identifier='com.serial-debug.app',
        info_plist={
            'CFBundleShortVersionString': '1.0.0',
            'CFBundleName': '串口调试助手',
            'NSHighResolutionCapable': True,
        },
    )
elif _platform == 'win32':
    # Windows: onedir + windowed
    exe = EXE(
        pyz, a.scripts, [],
        exclude_binaries=True,
        name='serial_debug',
        debug=False,
        strip=False,
        upx=True,
        console=False,
        icon='assets/icon.png',
        disable_windowed_traceback=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
    coll = COLLECT(
        exe, a.binaries, a.zipfiles, a.datas,
        name='serial_debug',
    )
else:
    # Linux: onedir（后续用 build_linux.sh 转 AppImage）
    exe = EXE(
        pyz, a.scripts, [],
        exclude_binaries=True,
        name='serial_debug',
        debug=False,
        strip=False,
        upx=True,
        console=False,
        icon='assets/icon.png',
    )
    coll = COLLECT(
        exe, a.binaries, a.zipfiles, a.datas,
        name='serial_debug',
    )
