# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec 文件 - 串口调试助手跨平台打包。

构建命令（在仓库根目录执行）：
  pyinstaller --clean packaging/serial_debug.spec

输出：
  Linux:   dist/serial_debug/        (onedir，后续用 build_linux.sh 转 AppImage)
  Windows: dist/serial_debug/       (onedir，可用 Inno Setup 打包成安装器)
  macOS:   dist/serial_debug.app     (app bundle)

图标：
  Linux:   .desktop 文件引用 serial_debug 资源目录的 PNG
  Windows: 嵌入 src/serial_debug/resources/assets/icon.ico
  macOS:   嵌入 src/serial_debug/resources/assets/icon.icns
"""
import os
import sys
from PyInstaller.utils.hooks import collect_submodules

# pyserial 延迟导入 list_ports 平台子模块，必须显式收集
hiddenimports = collect_submodules('serial')
# PySide6 + pyqtgraph：Qt 试点曲线窗口依赖（延迟导入，主程序不强制）
# 只收集核心子模块，避免 PySide6 全量打包膨胀体积（仍 ~80MB，可接受）
try:
    hiddenimports += collect_submodules('PySide6.QtCore')
    hiddenimports += collect_submodules('PySide6.QtGui')
    hiddenimports += collect_submodules('PySide6.QtWidgets')
except Exception:
    pass
hiddenimports += ['pyqtgraph', 'pyqtgraph.functions',
                  'pyqtgraph.graphicsItems', 'pyqtgraph.graphicsItems.PlotItem',
                  'pyqtgraph.graphicsItems.PlotDataItem',
                  'pyqtgraph.graphicsItems.ViewBox',
                  'pyqtgraph.graphicsItems.AxisItem',
                  'numpy', 'numpy.core', 'numpy.core._multiarray_umath']

# 仓库根目录（spec 在 packaging/ 下，向上一层）
_here = os.path.dirname(os.path.abspath(SPEC))
_root = os.path.dirname(_here)
_resources = os.path.join(_root, 'src', 'serial_debug', 'resources')

# 图标路径：按平台选择
_icon_path = None
if sys.platform == 'win32':
    _icon_path = os.path.join(_resources, 'assets', 'icon_debug.ico')
    if not os.path.exists(_icon_path):
        _icon_path = os.path.join(_resources, 'assets', 'icon_debug.png')
elif sys.platform == 'darwin':
    _icon_path = os.path.join(_resources, 'assets', 'icon_debug.icns')
    if not os.path.exists(_icon_path):
        _icon_path = os.path.join(_resources, 'assets', 'icon_debug.png')
else:
    _icon_path = os.path.join(_resources, 'assets', 'icon_debug.png')

a = Analysis(
    [os.path.join(_root, 'src', 'serial_debug', 'app.py')],
    pathex=[os.path.join(_root, 'src')],
    binaries=[],
    datas=[
        (os.path.join(_resources, 'assets'), 'serial_debug/resources/assets'),
        (os.path.join(_resources, 'common_at.json'), 'serial_debug/resources'),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=['matplotlib', 'pytest', 'IPython',
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
        icon=_icon_path,
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
        icon=_icon_path,
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
        icon=_icon_path,
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
        icon=_icon_path,
    )
    coll = COLLECT(
        exe, a.binaries, a.zipfiles, a.datas,
        name='serial_debug',
    )
